"""
전체 파이프라인 실행 진입점.

  1·2단계  수집(채널+검색, video_id 중복 제거)
  3단계    음성/자막 수집
  4단계    레시피 항목 추출
  5단계    CSV 저장 (REF_Youtube_Recipe_YYYYMMDD_HHMM.csv)

체크포인트(JSONL)를 써서 중간에 끊겨도 이어서 실행됨.
ASR 이 영상당 수십 초 걸리므로 재실행 비용을 줄이는 게 중요함.

사용:
    export YOUTUBE_API_KEY="..."
    python -m ytrecipe.pipeline --limit 50
    python -m ytrecipe.pipeline --collect-only     # 목록만 먼저 확보
    python -m ytrecipe.pipeline --no-asr           # 자막만으로 빠르게 테스트
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import crawler_config as C
from .youtube_video_list_collector import collect_all, fetch_pinned_comment, _client
from .recipe_field_extractor import extract_recipe
from .recipe_step_normalizer import build_processed_recipe
from .audio_transcriber_subtitle_fetcher import transcribe_video

logger = logging.getLogger("pipeline")

VIDEO_CACHE = C.CACHE_DIR / "videos.jsonl"        # 수집된 영상 목록
RESULT_CACHE = C.CACHE_DIR / "results.jsonl"      # 처리 완료 레코드


def _load_jsonl(path: Path) -> list[dict]:
    """JSONL 체크포인트 로드 (없으면 빈 리스트)."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue                            # 깨진 줄은 건너뜀
    return out


def _append_jsonl(path: Path, record: dict) -> None:
    """레코드 한 건을 JSONL 에 추가 (건건이 저장해 중단 대비)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def step_collect(force: bool = False) -> list[dict]:
    """1·2단계 — 영상 목록 확보(캐시 있으면 재사용)."""
    if VIDEO_CACHE.exists() and not force:
        videos = _load_jsonl(VIDEO_CACHE)
        logger.info("영상 목록 캐시 재사용: %d개", len(videos))
        return videos

    videos = collect_all()
    VIDEO_CACHE.parent.mkdir(parents=True, exist_ok=True)
    VIDEO_CACHE.write_text(
        "\n".join(json.dumps(v, ensure_ascii=False) for v in videos) + "\n",
        encoding="utf-8",
    )
    logger.info("영상 목록 저장: %d개 → %s", len(videos), VIDEO_CACHE)
    return videos


def step_process(videos: list[dict], limit: int | None, use_asr: bool,
                 fetch_pinned: bool, keep_audio: bool) -> list[dict]:
    """3·4단계 — 음성/자막 수집 후 레시피 추출. 처리분은 즉시 캐시에 기록."""
    done = {r["video_id"] for r in _load_jsonl(RESULT_CACHE)}
    todo = [v for v in videos if v["video_id"] not in done]
    if limit:
        todo = todo[:limit]
    logger.info("처리 대상 %d개 (완료분 %d개 스킵)", len(todo), len(done))

    yt = _client() if fetch_pinned else None

    for i, v in enumerate(todo, 1):
        vid = v["video_id"]
        logger.info("[%d/%d] %s — %s", i, len(todo), vid, v.get("video_title", "")[:40])
        rec = dict(v)

        # 고정댓글(추정) — API 에 isPinned 가 없어 휴리스틱 + 검증 플래그
        if yt is not None:
            text, verified = fetch_pinned_comment(yt, vid)
            rec["pinned_comment"] = text
            rec["is_pinned_verified"] = verified
        else:
            rec.setdefault("pinned_comment", "")
            rec.setdefault("is_pinned_verified", False)

        # 음성 + 자막
        if use_asr:
            rec.update(transcribe_video(vid, keep_audio=keep_audio))
        else:
            from .audio_transcriber_subtitle_fetcher import fetch_subtitle
            sub, is_manual, lang = fetch_subtitle(vid)
            rec.update(recipe_subtitle=sub, subtitle_is_manual=is_manual,
                       subtitle_lang=lang, recipe_audio="",
                       asr_avg_logprob=0.0, asr_model="")

        # 레시피 항목 추출 + 원문 정형화
        rec.update(extract_recipe(rec))
        rec.update(build_processed_recipe(rec))
        _append_jsonl(RESULT_CACHE, rec)            # 건건이 저장

    return _load_jsonl(RESULT_CACHE)


# 공개 배포 시 빼는 게 안전한 원문 컬럼 (저작권 이슈)
RAW_TEXT_COLUMNS = ["recipe_audio", "recipe_subtitle", "description", "pinned_comment"]


def step_save(records: list[dict], drop_raw: bool = False) -> Path:
    """5단계 — DataFrame 정리 후 CSV 저장."""
    df = pd.DataFrame(records)

    # 스키마 정렬 — 없는 컬럼은 빈 값으로 채워 순서 고정
    for col in C.CSV_COLUMNS:
        if col not in df.columns:
            df[col] = None
    cols = C.CSV_COLUMNS
    if drop_raw:                                  # 원문 제외본 (배포용)
        cols = [c for c in cols if c not in RAW_TEXT_COLUMNS]
        logger.info("원문 컬럼 제외: %s", ", ".join(RAW_TEXT_COLUMNS))
    df = df[cols]

    # video_id 기준 최종 중복 제거(캐시 중복 대비)
    before = len(df)
    df = df.drop_duplicates(subset="video_id", keep="first")
    if before != len(df):
        logger.info("중복 %d건 제거", before - len(df))

    C.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    path = C.OUTPUT_DIR / f"{C.CSV_PREFIX}_{stamp}.csv"
    # utf-8-sig: 엑셀에서 한글 깨짐 방지
    df.to_csv(path, index=False, encoding="utf-8-sig")

    logger.info("저장 완료: %s (%d행)", path, len(df))
    _print_summary(df)
    return path


def _print_summary(df: pd.DataFrame) -> None:
    """수집 품질 요약 — 재료 추출률·자막 유형 분포 등."""
    n = len(df)
    if n == 0:
        return
    has_ing = (df["ingredients"].fillna("") != "").sum()
    has_time = (df["cook_time_display"].fillna("") != "").sum()
    has_audio = (df["recipe_audio"].fillna("") != "").sum()
    manual = (df["subtitle_is_manual"] == True).sum()      # noqa: E712
    auto = (df["subtitle_is_manual"] == False).sum()       # noqa: E712
    none_sub = df["subtitle_is_manual"].isna().sum()
    pinned = (df["is_pinned_verified"] == True).sum()      # noqa: E712

    print("\n" + "=" * 52)
    print(f"총 {n}행")
    print(f"  재료 추출     {has_ing:>4} ({has_ing/n:.0%})")
    print(f"  조리시간 추출 {has_time:>4} ({has_time/n:.0%})")
    print(f"  음성 인식     {has_audio:>4} ({has_audio/n:.0%})")
    print(f"  자막: 수동 {manual} / 자동 {auto} / 없음 {none_sub}")
    print(f"  고정댓글 검증 {pinned:>4}")
    if "processed_step_count" in df.columns:
        proc = (df["processed_step_count"].fillna(0) > 0).sum()
        avg = df.loc[df["processed_step_count"] > 0, "processed_step_count"].mean()
        print(f"  정형화 성공   {proc:>4} ({proc/n:.0%}), 평균 {avg:.1f}단계" if proc else "  정형화 성공      0")
    print("=" * 52)


def main() -> None:
    ap = argparse.ArgumentParser(description="유튜브 자취요리 레시피 수집기")
    ap.add_argument("--limit", type=int, default=None, help="처리할 영상 수 제한(테스트용)")
    ap.add_argument("--collect-only", action="store_true", help="영상 목록만 수집하고 종료")
    ap.add_argument("--no-asr", action="store_true", help="음성 인식 생략(자막만, 빠름)")
    ap.add_argument("--no-pinned", action="store_true", help="고정댓글 수집 생략(쿼터 절약)")
    ap.add_argument("--keep-audio", action="store_true", help="음성 파일 삭제하지 않음")
    ap.add_argument("--refresh-list", action="store_true", help="영상 목록 캐시 무시하고 재수집")
    ap.add_argument("--drop-raw", action="store_true",
                    help="원문 텍스트 컬럼 제외하고 저장(공개 배포용)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    videos = step_collect(force=args.refresh_list)
    if args.collect_only:
        print(f"영상 목록 {len(videos)}개 수집 완료 → {VIDEO_CACHE}")
        return

    records = step_process(
        videos, limit=args.limit, use_asr=not args.no_asr,
        fetch_pinned=not args.no_pinned, keep_audio=args.keep_audio,
    )
    step_save(records, drop_raw=args.drop_raw)


if __name__ == "__main__":
    main()
