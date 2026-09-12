"""
크롤러 전역 설정.

채널 목록·검색 키워드·모델 이름 등 자주 바뀌는 값을 한곳에 모음.
API 키는 코드에 박지 않고 환경변수(YOUTUBE_API_KEY)로 주입함.
"""

from __future__ import annotations

import os
from pathlib import Path

# ── 인증 ─────────────────────────────────────────────────────────
# export YOUTUBE_API_KEY="..." 로 주입 (코드/깃에 키 노출 방지)
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")

# ── 경로 ─────────────────────────────────────────────────────────
# BASE_DIR : 이 코드 폴더(recipe_collection_model)
# 작업 파일(음성·캐시)은 코드 폴더 안 data/ 에 두고 git 에서 제외함.
# 최종 CSV 만 별도 데이터 폴더(recipe_data_collection)로 내보냄.
BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent

DATA_DIR = BASE_DIR / "data"            # 작업용(gitignore 대상)
AUDIO_DIR = DATA_DIR / "audio"          # 임시 음성 파일(처리 후 삭제 가능)
CACHE_DIR = DATA_DIR / "cache"          # 체크포인트(JSONL) — 재실행 시 스킵용

# 최종 CSV 저장 위치 — 데이터 폴더에는 CSV 만 들어감
OUTPUT_DIR = REPO_ROOT / "recipe_data_collection"

# ── 1단계: 자취/간단요리 전문 채널 ────────────────────────────────
# 핸들(@xxx) 또는 채널ID(UC...) 둘 다 허용. resolve 단계에서 채널ID로 변환함.
TARGET_CHANNELS: list[str] = [
    "@1minrecipe",        # 1분요리 뚝딱이형
    "@자취요리신",
    "@매일맛나막탕",
    "@하루한끼",
    "@요리용디",
    "@쿠킹하루",
    # 필요 시 추가 — 핸들은 채널 URL 의 @뒤 문자열
]

# ── 2단계: 검색 키워드 (채널 목록과 중복은 video_id 로 제거) ──────
SEARCH_QUERIES: list[str] = [
    "자취 요리 레시피",
    "자취생 요리",
    "간단 요리 레시피",
    "혼밥 레시피",
    "10분 요리",
    "에어프라이어 자취 요리",
    "전자레인지 요리",
]

# 채널당 최대 수집 영상 수 / 검색어당 최대 결과 수
MAX_VIDEOS_PER_CHANNEL = 100
MAX_RESULTS_PER_QUERY = 50

# ── 영상 필터 ────────────────────────────────────────────────────
MIN_DURATION_SEC = 30          # 너무 짧은 클립 제외
MAX_DURATION_SEC = 60 * 30     # 30분 초과 = 레시피 아닐 확률 높음(브이로그 등)
EXCLUDE_TITLE_KEYWORDS = ["먹방", "리뷰", "브이로그", "vlog", "mukbang", "언박싱"]

# ── 3단계: 음성 인식(ASR) ────────────────────────────────────────
ASR_MODEL_PRIMARY = "large-v3"          # 기본(정확도 우선)
ASR_MODEL_FALLBACK = "large-v3-turbo"   # VRAM 부족 시 대안
ASR_LANGUAGE = "ko"
ASR_BEAM_SIZE = 5
ASR_VAD_FILTER = True                   # 무음 구간 제거 → 환각 감소
# VRAM 임계값(GB). 이 미만이면 fallback 모델 + int8 로 자동 전환함.
VRAM_THRESHOLD_GB = 10.0

# ── 자막 ─────────────────────────────────────────────────────────
SUBTITLE_LANGS = ["ko", "en"]           # 선호 순서

# ── 출력 ─────────────────────────────────────────────────────────
CSV_PREFIX = "REF_Youtube_Recipe"       # 최종 파일명 접두사

# 최종 CSV 컬럼 순서 (영문 스네이크케이스)
CSV_COLUMNS: list[str] = [
    # 식별자
    "video_id", "video_url", "video_title", "channel_name", "channel_id",
    # 추출 결과
    "dish_name", "ingredients", "ingredients_json", "cook_time_display", "cook_time_min",
    "servings",
    # 정형화 레시피 (원문 사족 제거 → 번호 단계)
    "processed_recipe", "processed_step_count", "processed_source",
    # 원문 텍스트 (공개 배포 시 제외 권장)
    "recipe_audio", "recipe_subtitle", "description", "pinned_comment",
    # 품질/출처 메타
    "subtitle_is_manual", "subtitle_lang", "asr_model", "asr_avg_logprob",
    "extraction_source", "is_pinned_verified",
    # 영상 메타
    "duration_sec", "published_at", "view_count", "collected_at",
]
