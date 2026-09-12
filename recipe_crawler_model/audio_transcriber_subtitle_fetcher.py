"""
3단계 — 음성(ASR) 및 자막 수집.

베이스는 음성 인식(faster-whisper), 자막은 보조.
자막 신뢰도 구분이 핵심 요구사항이라 is_generated 플래그를 그대로 보존함.
  - 수동 자막(is_generated=False) : 업로더가 직접 작성 → 신뢰도 높음
  - 자동 자막(is_generated=True)  : 발음/잡음에 따라 품질 들쭉날쭉 → 참고용

ASR 품질 신호로 avg_logprob 평균을 같이 저장함.
값이 낮으면(-1.0 이하) 인식이 부정확할 가능성이 높으니 후단에서 걸러낼 수 있음.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import crawler_config as C

logger = logging.getLogger(__name__)

_model = None          # 모델 싱글턴 (로딩 비용이 커서 재사용)
_model_name = ""


# ──────────────────────────────────────────────────────────────────
# 자막
# ──────────────────────────────────────────────────────────────────
def fetch_subtitle(video_id: str) -> tuple[str, bool | None, str]:
    """
    자막 수집. 수동 자막 우선, 없으면 자동 자막.

    Returns:
        (자막 텍스트, is_manual(수동 여부), 언어코드)
        자막이 아예 없으면 ("", None, "")
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        logger.error("youtube-transcript-api 미설치")
        return "", None, ""

    try:
        api = YouTubeTranscriptApi()
        tlist = api.list(video_id)

        # 수동 자막 우선 탐색 → 실패 시 자동 자막
        transcript, is_manual = None, None
        try:
            transcript = tlist.find_manually_created_transcript(C.SUBTITLE_LANGS)
            is_manual = True
        except Exception:
            try:
                transcript = tlist.find_generated_transcript(C.SUBTITLE_LANGS)
                is_manual = False
            except Exception:
                return "", None, ""

        fetched = transcript.fetch()
        text = " ".join(s.text for s in fetched).strip()
        return text, is_manual, transcript.language_code
    except Exception as e:                       # 자막 비활성/영상 비공개 등
        logger.debug("자막 없음 %s: %s", video_id, e)
        return "", None, ""


# ──────────────────────────────────────────────────────────────────
# 음성 인식
# ──────────────────────────────────────────────────────────────────
def _pick_model() -> tuple[str, str, str]:
    """
    가용 VRAM 을 보고 (모델명, device, compute_type) 결정.

    VRAM 충분     → large-v3 + float16 (정확도 우선)
    VRAM 부족     → large-v3-turbo + int8_float16 (속도/메모리 절충)
    GPU 없음      → large-v3-turbo + int8 (CPU, 느리지만 동작)
    """
    try:
        import torch
        if torch.cuda.is_available():
            vram = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
            if vram >= C.VRAM_THRESHOLD_GB:
                return C.ASR_MODEL_PRIMARY, "cuda", "float16"
            logger.warning("VRAM %.1fGB — fallback 모델 사용", vram)
            return C.ASR_MODEL_FALLBACK, "cuda", "int8_float16"
    except ImportError:
        pass
    logger.warning("GPU 미감지 — CPU 모드(느림)")
    return C.ASR_MODEL_FALLBACK, "cpu", "int8"


def _get_model():
    """faster-whisper 모델 로드(싱글턴)."""
    global _model, _model_name
    if _model is None:
        from faster_whisper import WhisperModel
        name, device, ctype = _pick_model()
        logger.info("ASR 모델 로드: %s (%s/%s)", name, device, ctype)
        _model = WhisperModel(name, device=device, compute_type=ctype)
        _model_name = name
    return _model


def download_audio(video_id: str, out_dir: Path) -> Path | None:
    """
    yt-dlp 로 음성만 내려받음(용량 절약 위해 최저 오디오 포맷).

    주의: 유튜브 이용약관상 다운로드는 제한될 수 있음.
    연구·내부 분석 목적에 한정하고, 원본 음성/전문 재배포는 하지 말 것.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{video_id}.m4a"
    if target.exists():
        return target

    try:
        import yt_dlp
    except ImportError:
        logger.error("yt-dlp 미설치")
        return None

    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": str(out_dir / f"{video_id}.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    except Exception as e:
        logger.warning("음성 다운로드 실패 %s: %s", video_id, e)
        return None

    # 확장자가 다를 수 있어 glob 으로 실제 파일 탐색
    for p in out_dir.glob(f"{video_id}.*"):
        if p.suffix.lower() in (".m4a", ".webm", ".mp3", ".opus"):
            return p
    return None


def transcribe_audio(audio_path: Path) -> tuple[str, float, str]:
    """
    음성 파일을 텍스트로 변환.

    Returns:
        (텍스트, avg_logprob 평균(품질 신호), 사용 모델명)
    """
    try:
        model = _get_model()
        segments, _info = model.transcribe(
            str(audio_path),
            language=C.ASR_LANGUAGE,
            beam_size=C.ASR_BEAM_SIZE,
            vad_filter=C.ASR_VAD_FILTER,          # 무음 제거로 환각 감소
        )
        parts, logprobs = [], []
        for seg in segments:                       # 제너레이터라 순회해야 실제 처리됨
            parts.append(seg.text.strip())
            if seg.avg_logprob is not None:
                logprobs.append(seg.avg_logprob)
        text = " ".join(parts).strip()
        avg = sum(logprobs) / len(logprobs) if logprobs else 0.0
        return text, round(avg, 4), _model_name
    except Exception as e:
        logger.warning("ASR 실패 %s: %s", audio_path.name, e)
        return "", 0.0, _model_name


def transcribe_video(video_id: str, keep_audio: bool = False) -> dict:
    """
    영상 하나의 음성+자막 수집 결과를 딕셔너리로 반환.
    """
    result = {
        "recipe_audio": "", "asr_avg_logprob": 0.0, "asr_model": "",
        "recipe_subtitle": "", "subtitle_is_manual": None, "subtitle_lang": "",
    }

    # 자막 (가벼우므로 먼저)
    sub, is_manual, lang = fetch_subtitle(video_id)
    result.update(recipe_subtitle=sub, subtitle_is_manual=is_manual, subtitle_lang=lang)

    # 음성 (베이스)
    audio = download_audio(video_id, C.AUDIO_DIR)
    if audio:
        text, avg, model = transcribe_audio(audio)
        result.update(recipe_audio=text, asr_avg_logprob=avg, asr_model=model)
        if not keep_audio:
            audio.unlink(missing_ok=True)          # 디스크 절약
    return result
