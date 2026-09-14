"""
fetchers.py — 원문 텍스트 수집 (3·4단계).

수집 대상:
    설명란·고정댓글  → collectors.py 에서 API 호출 시 함께 확보됨
    음성(ASR)        → faster-whisper (2순위)
    자막             → youtube-transcript-api (3순위)

확장 구조:
    BaseFetcher 를 상속하면 새 원문 소스(예: 블로그 본문, 커뮤니티 게시글)를
    추가할 수 있음. fetch() 가 dict 를 반환하면 레코드에 그대로 병합됨.

자막 신뢰도:
    youtube-transcript-api 의 is_generated 플래그로 수동/자동 자막을 구분함.
    수동 자막은 업로더가 직접 작성한 것이라 신뢰도가 높고,
    자동 자막은 발음·잡음에 따라 품질이 들쭉날쭉하므로 최하위 순위로 둠.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

from . import settings as S

logger = logging.getLogger(__name__)


class BaseFetcher(ABC):
    """원문 수집기 공통 인터페이스."""

    name: str = "base"

    @abstractmethod
    def fetch(self, video_id: str) -> dict:
        """레코드에 병합할 필드 dict 반환."""
        raise NotImplementedError


# ══════════════════════════════════════════════════════════════════
# 자막
# ══════════════════════════════════════════════════════════════════
class SubtitleFetcher(BaseFetcher):
    """
    유튜브 자막 수집. 수동 자막 우선, 없으면 자동 자막.

    반환 필드: recipe_subtitle, subtitle_is_manual, subtitle_lang
    """

    name = "subtitle"

    def __init__(self, langs: list[str] | None = None) -> None:
        self.langs = langs or S.SUBTITLE_LANGS

    def fetch(self, video_id: str) -> dict:
        empty = {"recipe_subtitle": "", "subtitle_is_manual": None,
                 "subtitle_lang": ""}
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError:
            logger.error("youtube-transcript-api 미설치")
            return empty

        try:
            api = YouTubeTranscriptApi()
            listing = api.list(video_id)

            transcript, is_manual = None, None
            try:
                transcript = listing.find_manually_created_transcript(self.langs)
                is_manual = True
            except Exception:
                try:
                    transcript = listing.find_generated_transcript(self.langs)
                    is_manual = False
                except Exception:
                    return empty

            fetched = transcript.fetch()
            text = " ".join(seg.text for seg in fetched).strip()
            return {
                "recipe_subtitle": text,
                "subtitle_is_manual": is_manual,
                "subtitle_lang": transcript.language_code,
            }
        except Exception as e:      # 자막 비활성·비공개 영상 등
            logger.debug("자막 없음 %s: %s", video_id, e)
            return empty


# ══════════════════════════════════════════════════════════════════
# 음성 인식
# ══════════════════════════════════════════════════════════════════
class AudioFetcher(BaseFetcher):
    """
    yt-dlp 로 음성을 받고 faster-whisper 로 텍스트 변환.

    반환 필드: recipe_audio, asr_model, asr_confidence

    모델은 인스턴스에 캐싱함(로딩 비용이 크므로 영상마다 재로딩하면 안 됨).
    """

    name = "audio"

    def __init__(self, keep_audio: bool = False) -> None:
        self.keep_audio = keep_audio
        self._model = None
        self._model_name = ""

    # ── 모델 선택 ────────────────────────────────────────────────
    def _pick_model(self) -> tuple[str, str, str]:
        """
        가용 VRAM 에 따라 (모델명, device, compute_type) 결정.

        VRAM 충분 → large-v3 + float16      (정확도 우선)
        VRAM 부족 → large-v3-turbo + int8   (메모리 절충)
        GPU 없음  → large-v3-turbo + int8   (CPU, 느림)
        """
        try:
            import torch
            if torch.cuda.is_available():
                vram = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
                if vram >= S.VRAM_THRESHOLD_GB:
                    return S.ASR_MODEL_PRIMARY, "cuda", "float16"
                logger.warning("VRAM %.1fGB — fallback 모델 사용", vram)
                return S.ASR_MODEL_FALLBACK, "cuda", "int8_float16"
        except ImportError:
            pass
        logger.warning("GPU 미감지 — CPU 모드(느림)")
        return S.ASR_MODEL_FALLBACK, "cpu", "int8"

    @property
    def model(self):
        """모델 지연 로딩."""
        if self._model is None:
            from faster_whisper import WhisperModel
            name, device, ctype = self._pick_model()
            logger.info("ASR 모델 로드: %s (%s/%s)", name, device, ctype)
            self._model = WhisperModel(name, device=device, compute_type=ctype)
            self._model_name = name
        return self._model

    # ── 음성 다운로드 ────────────────────────────────────────────
    def _download(self, video_id: str) -> Path | None:
        """
        yt-dlp 로 음성만 내려받음.

        주의: 유튜브 약관상 다운로드는 제한될 수 있음.
        수집한 음성은 텍스트 변환 후 삭제되며(기본값), 원문 재배포 금지.
        """
        out_dir = S.AUDIO_DIR
        out_dir.mkdir(parents=True, exist_ok=True)

        for ext in (".m4a", ".webm", ".mp3", ".opus"):
            p = out_dir / f"{video_id}{ext}"
            if p.exists():
                return p

        try:
            import yt_dlp
        except ImportError:
            logger.error("yt-dlp 미설치")
            return None

        opts = {
            "format": "bestaudio[ext=m4a]/bestaudio",
            "outtmpl": str(out_dir / f"{video_id}.%(ext)s"),
            "quiet": True, "no_warnings": True, "noprogress": True, "retries": 3,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        except Exception as e:
            logger.warning("음성 다운로드 실패 %s: %s", video_id, e)
            return None

        for p in out_dir.glob(f"{video_id}.*"):
            if p.suffix.lower() in (".m4a", ".webm", ".mp3", ".opus"):
                return p
        return None

    # ── 변환 ─────────────────────────────────────────────────────
    def fetch(self, video_id: str) -> dict:
        empty = {"recipe_audio": "", "asr_model": "", "asr_confidence": 0.0}
        audio = self._download(video_id)
        if not audio:
            return empty

        try:
            segments, _info = self.model.transcribe(
                str(audio),
                language=S.ASR_LANGUAGE,
                beam_size=S.ASR_BEAM_SIZE,
                vad_filter=S.ASR_VAD_FILTER,
            )
            parts, logprobs = [], []
            for seg in segments:            # 제너레이터라 순회해야 실제 처리됨
                parts.append(seg.text.strip())
                if seg.avg_logprob is not None:
                    logprobs.append(seg.avg_logprob)
            text = " ".join(parts).strip()
            avg = round(sum(logprobs) / len(logprobs), 4) if logprobs else 0.0
            return {
                "recipe_audio": text,
                "asr_model": self._model_name,
                "asr_confidence": avg,
            }
        except Exception as e:
            logger.warning("ASR 실패 %s: %s", video_id, e)
            return empty
        finally:
            if not self.keep_audio and audio.exists():
                audio.unlink(missing_ok=True)       # 디스크 절약


FETCHER_REGISTRY: dict[str, type[BaseFetcher]] = {
    SubtitleFetcher.name: SubtitleFetcher,
    AudioFetcher.name: AudioFetcher,
}
