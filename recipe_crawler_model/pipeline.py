"""
pipeline.py — 전체 파이프라인 오케스트레이션.

단계:
    1·2. 영상 목록 수집 (채널 + 검색, video_id 중복 제거)
    3.   설명란·고정댓글 확보 (레시피 정리본 판별 포함)
    4.   음성(ASR) + 자막 수집
    5.   필드 추출 (요리명·재료·조리시간) + 재료 정규화
    6.   레시피 정제 (recipe_processed)
    7.   CSV 저장

체크포인트:
    처리 결과를 건건이 JSONL 에 기록해 중단 후 재실행 시 처리분을 건너뜀.
    ASR 이 영상당 수십 초 걸리므로 재실행 비용을 줄이는 게 중요함.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import collectors, extractors, settings as S
from .fetchers import AudioFetcher, SubtitleFetcher
from .processors import get_processor
from .schema import CSV_COLUMNS, RAW_TEXT_COLUMNS, RecipeRecord

logger = logging.getLogger(__name__)

VIDEO_CACHE = S.CACHE_DIR / "videos.jsonl"
RESULT_CACHE = S.CACHE_DIR / "results.jsonl"


# ══════════════════════════════════════════════════════════════════
# 체크포인트 유틸
# ══════════════════════════════════════════════════════════════════
def load_jsonl(path: Path) -> list[dict]:
    """JSONL 로드 (깨진 줄은 건너뜀)."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def append_jsonl(path: Path, record: dict) -> None:
    """레코드 한 건 추가 (중단 대비 건건이 저장)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# ══════════════════════════════════════════════════════════════════
# 단계별 실행
# ══════════════════════════════════════════════════════════════════
def step_collect(config) -> list[dict]:
    """1·2단계 — 영상 목록 확보 (캐시 재사용)."""
    if VIDEO_CACHE.exists() and not config.refresh_list:
        videos = load_jsonl(VIDEO_CACHE)
        logger.info("영상 목록 캐시 재사용: %d개", len(videos))
        return videos

    videos = collectors.collect_videos(config)
    VIDEO_CACHE.parent.mkdir(parents=True, exist_ok=True)
    VIDEO_CACHE.write_text(
        "\n".join(json.dumps(v, ensure_ascii=False) for v in videos) + "\n",
        encoding="utf-8",
    )
    logger.info("영상 목록 저장: %d개 → %s", len(videos), VIDEO_CACHE)
    return videos


def step_process(videos: list[dict], config) -> list[dict]:
    """3~6단계 — 원문 수집 → 추출 → 정제."""
    done = {r.get("video_id") for r in load_jsonl(RESULT_CACHE)}
    todo = [v for v in videos if v.get("video_id") not in done]
    if config.limit:
        todo = todo[:config.limit]
    logger.info("처리 대상 %d개 (완료분 %d개 스킵)", len(todo), len(done))

    # 무거운 객체는 루프 밖에서 1회만 생성
    client = collectors.build_client() if config.fetch_comment else None
    subtitle_fetcher = SubtitleFetcher()
    audio_fetcher = AudioFetcher(keep_audio=config.keep_audio) if config.use_asr else None
    processor = get_processor(config.processor)

    for i, video in enumerate(todo, 1):
        vid = video.get("video_id", "")
        logger.info("[%d/%d] %s — %s", i, len(todo), vid,
                    video.get("video_title", "")[:40])

        record = RecipeRecord().update(video)

        # 3단계: 고정댓글 (설명란은 수집 단계에서 이미 확보됨)
        if client is not None:
            text, verified = collectors.fetch_pinned_comment(
                client, vid, video.get("channel_id", "")
            )
            record.recipe_comment = text
            record.comment_is_uploader = verified

        # 4단계: 자막 + 음성
        record.update(subtitle_fetcher.fetch(vid))
        if audio_fetcher is not None:
            record.update(audio_fetcher.fetch(vid))

        # 5단계: 필드 추출 (요리명·재료·시간 + 정규화)
        extractors.run_all(record)

        # 6단계: 레시피 정제
        record.update(processor.process(record))

        append_jsonl(RESULT_CACHE, record.to_row())

    return load_jsonl(RESULT_CACHE)


def step_save(records: list[dict], config) -> Path:
    """7단계 — CSV 저장."""
    df = pd.DataFrame(records)
    if df.empty:
        logger.warning("저장할 레코드 없음")
        return S.OUTPUT_DIR

    # 스키마 정렬 — 없는 컬럼은 빈 값으로 채워 순서 고정
    for col in CSV_COLUMNS:
        if col not in df.columns:
            df[col] = None
    cols = CSV_COLUMNS
    if config.drop_raw:
        cols = [c for c in cols if c not in RAW_TEXT_COLUMNS]
        logger.info("원문 컬럼 제외: %s", ", ".join(RAW_TEXT_COLUMNS))
    df = df[cols]

    # video_id 기준 최종 중복 제거
    before = len(df)
    df = df.drop_duplicates(subset="video_id", keep="first")
    if before != len(df):
        logger.info("중복 %d건 제거", before - len(df))

    S.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    path = S.OUTPUT_DIR / f"{S.CSV_PREFIX}_{stamp}.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")   # 엑셀 한글 대응

    logger.info("저장 완료: %s (%d행)", path, len(df))
    print_summary(df)
    return path


def print_summary(df: pd.DataFrame) -> None:
    """수집 품질 요약 — 어느 소스가 실제로 기여했는지 확인용."""
    n = len(df)
    if n == 0:
        return

    def filled(col: str) -> int:
        return int((df[col].fillna("").astype(str) != "").sum()) if col in df else 0

    print("\n" + "=" * 56)
    print(f"총 {n}행")
    print(f"  재료 추출       {filled('recipe_ingredients'):>4} ({filled('recipe_ingredients')/n:.0%})")
    print(f"  조리시간 추출   {filled('cook_time'):>4} ({filled('cook_time')/n:.0%})")
    print(f"  정제 레시피     {filled('recipe_processed'):>4} ({filled('recipe_processed')/n:.0%})")
    print(f"  음성 인식       {filled('recipe_audio'):>4}")

    if "main_ingredient_count" in df:
        avg = pd.to_numeric(df["main_ingredient_count"], errors="coerce").fillna(0)
        print(f"  평균 주재료 수  {avg[avg > 0].mean():.1f}개" if (avg > 0).any()
              else "  평균 주재료 수  0")

    if "desc_has_recipe" in df:
        print(f"  레시피 정리본   설명란 {int(df['desc_has_recipe'].sum())} / "
              f"고정댓글 {int(df.get('comment_has_recipe', pd.Series()).sum() or 0)}")

    if "subtitle_is_manual" in df:
        manual = int((df["subtitle_is_manual"] == True).sum())    # noqa: E712
        auto = int((df["subtitle_is_manual"] == False).sum())     # noqa: E712
        print(f"  자막            수동 {manual} / 자동 {auto} / 없음 {n - manual - auto}")

    for col, label in (("ingredients_source", "재료 출처"),
                       ("processed_source", "정제 출처")):
        if col in df:
            counts = df[col].fillna("").replace("", "없음").value_counts().head(4)
            print(f"  {label}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print("=" * 56)


def run(config) -> Path | None:
    """전체 파이프라인 실행."""
    videos = step_collect(config)
    if config.collect_only:
        print(f"영상 목록 {len(videos)}개 수집 완료 → {VIDEO_CACHE}")
        return None
    records = step_process(videos, config)
    return step_save(records, config)
