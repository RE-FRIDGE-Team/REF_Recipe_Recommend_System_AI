"""
schema.py — CSV 컬럼 정의 단일 소스.

컬럼을 추가·변경할 때 이 파일만 고치면 파이프라인 전체에 반영됨.
(추출기·저장기가 모두 여기 정의를 참조하므로 하드코딩된 컬럼명이 흩어지지 않음)

설계 메모:
    추천 시스템이 냉장고 재고와 대조해야 하므로, 재료를 문자열 하나로만 두지 않고
    아래 3중 구조로 저장함.
      recipe_ingredients  — 사람이 읽는 원문 ("돼지고기 목살 300g, 마늘 3개")
      ingredients_json    — 구조화 [{name, amount, unit, role}]
      main_ingredients    — 매칭용 주재료명만, PGIN 키 기준 ("목살|마늘")
      main_ingredient_mediums — 주재료별 중분류. 해석 근거 확인용.
    카테고리는 '대체' 가 아니라 '해석' 에 씀. 레시피가 "돼지고기 목살" 이라
    적어도 재고 키인 PGIN "목살" 로 모이도록 카테고리명을 단서로 활용함.
    양념(간장·소금 등)은 대부분 상비 품목이라 매칭 분모에서 빼야 추천이 실용적임.
    그래서 main / seasoning 을 분리해 둠.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── 원문 텍스트 컬럼 (공개 배포 시 제외 대상) ────────────────────
RAW_TEXT_COLUMNS: list[str] = [
    "recipe_desc",
    "recipe_comment",
    "recipe_audio",
    "recipe_subtitle",
]

# ── 최종 CSV 컬럼 순서 ───────────────────────────────────────────
CSV_COLUMNS: list[str] = [
    # ── 식별 ──
    "video_id",            # 중복 판정 1차 키(불변). 제목·채널보다 신뢰도 높음
    "video_title",         # 영상 제목
    "channel_name",        # 채널 이름 (중복 2차 확인)
    "video_url",           # 영상 링크

    # ── 레시피 핵심 ──
    "recipe_name",         # 요리 이름 (중복 요리 확인)
    "recipe_ingredients",  # 재료 원문 ("마늘 3개, 간장 3T")
    "cook_time",           # 조리 시간 표시용 ("약 15분")
    "recipe_processed",    # 정제된 번호 단계 레시피

    # ── 추천 시스템 연동용 (구조화) ──
    "ingredients_json",    # [{name, amount, unit, role}] JSON
    "main_ingredients",    # 주재료명만 파이프 구분 ("목살|마늘|대파")
    "seasoning_ingredients",  # 양념명만 파이프 구분 ("간장|미림")
    "main_ingredient_count",  # 주재료 개수 (매칭률 분모)
    "main_ingredient_mediums",  # 주재료별 중분류 (main 과 순서 일치)
    "unresolved_ingredients",   # PGIN 에 없던 재료 (어휘 보강 후보)
    "cook_time_minutes",   # 조리 시간 숫자 (정렬·필터용)
    "servings",            # 인분

    # ── 원문 ──
    "recipe_desc",         # 유튜브 설명란 (1순위 소스)
    "recipe_comment",      # 고정 댓글 (1순위 소스)
    "recipe_audio",        # 음성 인식 (2순위)
    "recipe_subtitle",     # 자막 (3순위)

    # ── 품질·출처 메타 ──
    "desc_has_recipe",       # 설명란이 레시피 정리본인지 판별 결과
    "comment_has_recipe",    # 고정댓글이 레시피 정리본인지
    "comment_is_uploader",   # 고정댓글 작성자가 업로더인지(검증 플래그)
    "subtitle_is_manual",    # 수동 자막(True)/자동(False)/없음(None)
    "subtitle_lang",
    "asr_model",             # 사용한 whisper 모델
    "asr_confidence",        # ASR 품질 신호(avg_logprob)
    "ingredients_source",    # 재료를 뽑은 소스
    "processed_source",      # 정제 레시피를 뽑은 소스
    "processed_by",          # rule | llm | manual

    # ── 영상 메타 ──
    "duration_sec",
    "published_at",
    "view_count",
    "collected_at",
]


@dataclass
class RecipeRecord:
    """
    한 영상에 대응하는 레코드.

    파이프라인 단계마다 필드를 채워 나가는 컨테이너.
    dict 를 그냥 쓰면 오타로 컬럼이 새는 일이 잦아 dataclass 로 고정함.
    새 컬럼을 추가할 때는 CSV_COLUMNS 와 여기 둘 다 추가할 것.
    """
    # 식별
    video_id: str = ""
    video_title: str = ""
    channel_name: str = ""
    channel_id: str = ""
    video_url: str = ""

    # 레시피 핵심
    recipe_name: str = ""
    recipe_ingredients: str = ""
    cook_time: str = ""
    recipe_processed: str = ""

    # 구조화
    ingredients_json: str = ""
    main_ingredients: str = ""
    seasoning_ingredients: str = ""
    main_ingredient_count: int = 0
    main_ingredient_mediums: str = ""
    unresolved_ingredients: str = ""
    cook_time_minutes: int | None = None
    servings: str = ""

    # 원문
    recipe_desc: str = ""
    recipe_comment: str = ""
    recipe_audio: str = ""
    recipe_subtitle: str = ""

    # 품질·출처
    desc_has_recipe: bool = False
    comment_has_recipe: bool = False
    comment_is_uploader: bool = False
    subtitle_is_manual: bool | None = None
    subtitle_lang: str = ""
    asr_model: str = ""
    asr_confidence: float = 0.0
    ingredients_source: str = ""
    processed_source: str = ""
    processed_by: str = ""

    # 영상 메타
    duration_sec: int = 0
    published_at: str = ""
    view_count: int = 0
    collected_at: str = ""

    # 파이프라인 내부용(CSV 에는 안 나감)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        """CSV 한 행으로 변환 (CSV_COLUMNS 에 있는 필드만)."""
        d = self.__dict__
        return {c: d.get(c, "") for c in CSV_COLUMNS}

    def update(self, data: dict[str, Any]) -> "RecipeRecord":
        """딕셔너리로 필드 일괄 갱신 (정의된 필드만 반영)."""
        for k, v in data.items():
            if hasattr(self, k):
                setattr(self, k, v)
            else:
                self.extra[k] = v
        return self
