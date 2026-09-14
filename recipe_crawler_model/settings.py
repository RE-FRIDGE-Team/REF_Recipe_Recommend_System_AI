"""
settings.py — 전역 설정.

자주 바뀌는 값(채널 목록, 검색어, 모델, 임계값)을 한곳에 모음.
API 키는 코드에 박지 않고 .env / 환경변수로 주입함.

확장 메모:
    새 수집 대상(채널·키워드)을 늘릴 때 이 파일만 수정하면 됨.
    동작 로직은 collectors.py 가 담당하므로 데이터와 로직이 분리돼 있음.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# .env 자동 로드 (python-dotenv 있으면 사용, 없으면 환경변수만)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ── 인증 ─────────────────────────────────────────────────────────
YOUTUBE_API_KEY: str = os.environ.get("YOUTUBE_API_KEY", "")
# LLM 정제를 쓸 때만 필요 (없으면 규칙 기반으로 동작)
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")


# ── 경로 ─────────────────────────────────────────────────────────
# 작업 파일(음성·캐시)은 코드 폴더 안 data/ 에 두고 git 제외.
# 최종 CSV 만 상위의 공용 데이터 폴더로 내보냄.
BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent

DATA_DIR = BASE_DIR / "data"
AUDIO_DIR = DATA_DIR / "audio"       # 임시 음성 (처리 후 삭제)
CACHE_DIR = DATA_DIR / "cache"       # 체크포인트 JSONL
OUTPUT_DIR = REPO_ROOT / "recipe_data_collection"   # 최종 CSV

CSV_PREFIX = "REF_Youtube_Recipe"


# ── 1단계: 자취/간단요리 전문 채널 ────────────────────────────────
# 핸들(@xxx) 또는 채널ID(UC...) 모두 허용. 실행 시 채널ID로 정규화함.
TARGET_CHANNELS: list[str] = [
    "@1분요리뚝딱이",
    "@자취요리신",
    "@매일맛나",
    "@하루한끼",
    "@요리용디",
    "@쿠킹하루",
]

# ── 2단계: 검색 키워드 (채널 수집분과 video_id 로 중복 제거) ──────
SEARCH_QUERIES: list[str] = [
    "자취 요리 레시피",
    "자취생 요리",
    "간단 요리 레시피",
    "혼밥 레시피",
    "10분 요리",
    "에어프라이어 자취 요리",
    "전자레인지 요리",
]

MAX_VIDEOS_PER_CHANNEL = 100
MAX_RESULTS_PER_QUERY = 50


# ── 영상 필터 ────────────────────────────────────────────────────
MIN_DURATION_SEC = 30
MAX_DURATION_SEC = 60 * 30          # 30분 초과는 브이로그일 확률 높음
EXCLUDE_TITLE_KEYWORDS = [
    "먹방", "리뷰", "브이로그", "vlog", "mukbang", "언박싱", "챌린지",
]


# ── 음성 인식 ────────────────────────────────────────────────────
ASR_MODEL_PRIMARY = "large-v3"         # 정확도 우선
ASR_MODEL_FALLBACK = "large-v3-turbo"  # VRAM 부족 시
ASR_LANGUAGE = "ko"
ASR_BEAM_SIZE = 5
ASR_VAD_FILTER = True                  # 무음 제거 → 환각 감소
VRAM_THRESHOLD_GB = 10.0


# ── 자막 ─────────────────────────────────────────────────────────
SUBTITLE_LANGS = ["ko", "en"]


# ── 소스 우선순위 ────────────────────────────────────────────────
# 앞쪽일수록 신뢰도 높음. 순서를 바꾸면 추출 우선순위가 바뀜.
# (업로더가 정리해 둔 텍스트 > 음성 > 자막)
SOURCE_PRIORITY: list[str] = [
    "recipe_comment",   # 고정댓글 정리본
    "recipe_desc",      # 설명란 정리본
    "recipe_audio",     # 음성 인식
    "recipe_subtitle",  # 자막
]


# ── 레시피 정리본 판별 임계값 ────────────────────────────────────
# 설명란/댓글이 '레시피 정리본'인지 판정할 때 쓰는 기준.
RECIPE_DETECT_MIN_INGREDIENTS = 2      # 계량 표현 최소 개수
RECIPE_DETECT_MIN_LENGTH = 30          # 최소 글자 수


@dataclass
class RunConfig:
    """
    1회 실행 옵션. CLI 인자가 여기에 매핑됨.

    확장 메모: 새 실행 옵션을 추가할 때 이 dataclass 에 필드를 넣고
    __main__.py 의 argparse 에 연결하면 파이프라인 전역에서 참조 가능함.
    """
    limit: int | None = None            # 처리할 영상 수 제한
    use_asr: bool = True                # 음성 인식 사용 여부
    fetch_comment: bool = True          # 고정댓글 수집 여부(쿼터 절약용 토글)
    keep_audio: bool = False            # 음성 파일 보존
    drop_raw: bool = False              # 원문 컬럼 제외하고 저장
    processor: str = "rule"             # rule | llm
    refresh_list: bool = False          # 영상 목록 캐시 무시
    collect_only: bool = False          # 목록 수집만 하고 종료
    channels: list[str] = field(default_factory=lambda: list(TARGET_CHANNELS))
    queries: list[str] = field(default_factory=lambda: list(SEARCH_QUERIES))
