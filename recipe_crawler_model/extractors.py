"""
extractors.py — 레시피 필드 추출 (5단계).

추출 대상:
    요리 이름 (영상 제목 기반)
    재료      (계량 표현 패턴)
    조리 시간
    인분
    + 설명란/댓글이 '레시피 정리본' 인지 판별

확장 구조:
    BaseExtractor 를 상속해 새 필드 추출기(난이도, 도구, 칼로리 등)를 추가할 수 있음.
    EXTRACTOR_REGISTRY 에 등록하면 파이프라인이 순회하며 호출함.

소스 우선순위:
    settings.SOURCE_PRIORITY 순서대로 훑되, 재료를 가장 많이 뽑아낸 소스를 채택함.
    업로더가 정리해 둔 텍스트(댓글·설명란)가 구어체 음성보다 정확하기 때문.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from . import settings as S

# ══════════════════════════════════════════════════════════════════
# 공통 패턴
# ══════════════════════════════════════════════════════════════════
# 한국어 레시피에 흔한 계량 단위. 재료 줄 판별의 핵심 신호.
UNITS = (
    r"g|kg|ml|mL|L|cc|"
    r"큰술|작은술|숟갈|스푼|티스푼|테이블스푼|컵|줌|꼬집|"
    r"개|알|쪽|장|줄|대|통|봉지|봉|팩|캔|마리|공기|인분|모|단|손|"
    r"T|t|Ts|ts"
)

# 정형 목록용 (줄 시작 기준). 재료명에 숫자가 못 들어가게 막아
# "3큰술이랑 미림" 같은 흡수를 방지함.
_ING_LINE = re.compile(
    rf"^[\s\-•*·▶▪◾>]*"
    rf"(?P<name>[가-힣A-Za-z][^:：\n\d]{{0,20}}?)\s*"
    rf"(?P<amount>\d+(?:[./~-]\d+)?)\s*"
    rf"(?P<unit>{UNITS})\b",
    re.MULTILINE,
)

# 구어체 연속 문장용 (줄 앵커 없음). 오탐을 줄이려 이름을 한글 1~10자로 제한.
_ING_INLINE = re.compile(
    rf"(?P<name>[가-힣]{{1,10}})\s*"
    rf"(?P<amount>\d+(?:[./~-]\d+)?)\s*"
    rf"(?P<unit>{UNITS})",
)

# 재료명에 오면 안 되는 표현 (감탄·잡담·조리도구)
_NOT_INGREDIENT = re.compile(
    r"맛있|진짜|정말|너무|완전|대박|좋아|저는|제가|여러분|오늘|영상|"
    r"구독|좋아요|댓글|채널|링크|"
    r"후라이팬|프라이팬|팬에|냄비|그릇|접시|볼에|도마|불로|불에"
)

# 재료명으로 올 수 없는 단위·시간 표현 (정확히 일치할 때만 배제)
_UNIT_ONLY_WORDS = {
    "분", "초", "시간", "인분", "정도", "동안", "총", "약", "면", "앞뒤",
    "한면", "한 면", "중불", "약불", "강불", "중강불",
}

_ING_HEADER = re.compile(r"(재\s*료|준비\s*물|材料|ingredients?)\s*[:：]?", re.I)

_TIME = re.compile(
    r"(?:조리\s*시간|소요\s*시간|총\s*시간)?\s*(?:약\s*)?"
    r"(?:(?P<hour>\d+)\s*시간)?\s*"
    r"(?:(?P<min1>\d+)\s*[~-]\s*(?P<min2>\d+)|(?P<min>\d+))?\s*분"
)

_SERVINGS = re.compile(r"(\d+(?:\s*[~-]\s*\d+)?)\s*인분")

# 제목에서 걷어낼 홍보·잡음
_TITLE_NOISE = re.compile(
    r"\[[^\]]*\]|\([^)]*\)|#\S+|[|｜/]+|"
    r"(?:구독|좋아요|레시피|만들기|만드는\s*법|초간단|간단|자취|혼밥|"
    r"asmr|ASMR|shorts|Shorts|SHORTS)|"
    r"\d+\s*(?:분|초)\s*(?:완성|컷|요리)?|"
    r"[Ee][Pp]\.?\s*\d+"
)


class BaseExtractor(ABC):
    """필드 추출기 공통 인터페이스."""

    name: str = "base"

    @abstractmethod
    def extract(self, record) -> dict:
        """레코드에 병합할 필드 dict 반환."""
        raise NotImplementedError


# ══════════════════════════════════════════════════════════════════
# 레시피 정리본 판별
# ══════════════════════════════════════════════════════════════════
def looks_like_recipe(text: str) -> bool:
    """
    설명란/댓글이 '레시피 정리본' 인지 판별.

    판정 근거:
        계량 표현(숫자+단위)이 임계값 이상 등장하거나,
        재료 섹션 헤더('재료:', '준비물')가 있는 경우.
    홍보 문구만 잔뜩 있는 설명란을 걸러내는 용도임.
    """
    if not text or len(text) < S.RECIPE_DETECT_MIN_LENGTH:
        return False
    if _ING_HEADER.search(text):
        return True
    hits = len(_ING_LINE.findall(text)) + len(_ING_INLINE.findall(text))
    return hits >= S.RECIPE_DETECT_MIN_INGREDIENTS


# ══════════════════════════════════════════════════════════════════
# 개별 추출기
# ══════════════════════════════════════════════════════════════════
class DishNameExtractor(BaseExtractor):
    """요리 이름 추출. 영상 제목 기반이 가장 안정적임."""

    name = "recipe_name"

    def extract(self, record) -> dict:
        title = getattr(record, "video_title", "")
        if not title:
            return {"recipe_name": ""}
        name = _TITLE_NOISE.sub(" ", title)
        name = re.sub(r"[^\w가-힣\s]", " ", name)
        name = re.sub(r"\s+", " ", name).strip()
        parts = name.split()
        if len(parts) > 6:                  # 제목에 설명이 길게 붙는 경우 대비
            name = " ".join(parts[:6])
        return {"recipe_name": name}


class IngredientExtractor(BaseExtractor):
    """
    재료 추출 + 정규화 + 주재료/양념 분류.

    소스 우선순위대로 훑되 재료를 가장 많이 뽑은 소스를 채택함.
    정규화·분류는 ingredient_normalizer 에 위임 (추천 시스템과 키를 맞추기 위함).
    """

    name = "ingredients"

    @staticmethod
    def parse(text: str, max_items: int = 30) -> list[dict]:
        """텍스트에서 재료 후보 추출."""
        if not text:
            return []

        # 재료 섹션 헤더가 있으면 그 이후 구간을 우선 대상으로
        m = _ING_HEADER.search(text)
        scope = text[m.end(): m.end() + 800] if m else text

        # 정형 목록·구어체 양쪽 시도 후 많이 뽑힌 쪽 채택
        line_hits = list(_ING_LINE.finditer(scope))
        inline_hits = list(_ING_INLINE.finditer(scope))
        matches = line_hits if len(line_hits) >= len(inline_hits) else inline_hits

        found: list[dict] = []
        seen: set[str] = set()
        for mm in matches:
            name = mm.group("name").strip(" -•*·▶▪◾>\t")
            name = re.split(r"[.!?…]", name)[-1]        # 앞 문장 잔재 제거
            name = re.sub(
                r"^.*?(?:넣고|넣어|하고|그리고|그\s*다음에?|이제|일단|먼저|"
                r"준비한|썰어둔|다져둔)\s+", "", name).strip()
            toks = name.split()
            if len(toks) > 2:                            # 재료명은 보통 1~2어절
                name = " ".join(toks[-2:])
            if not name or name.isdigit() or len(name) > 25:
                continue
            if _NOT_INGREDIENT.search(name):
                continue
            if name.replace(" ", "") in _UNIT_ONLY_WORDS:   # "3분씩" 의 "분" 등
                continue
            key = name.replace(" ", "")
            if key in seen:
                continue
            seen.add(key)
            found.append({
                "name": name,
                "amount": mm.group("amount"),
                "unit": mm.group("unit"),
                "raw": mm.group(0).strip(" -•*·▶▪◾>\t"),
            })
            if len(found) >= max_items:
                break
        return found

    def extract(self, record) -> dict:
        from .ingredient_normalizer import classify

        best: list[dict] = []
        best_source = ""
        for source in S.SOURCE_PRIORITY:
            text = getattr(record, source, "")
            items = self.parse(text)
            if len(items) > len(best):
                best, best_source = items, source
            if len(best) >= 4:              # 충분히 뽑았으면 조기 종료
                break

        result = {
            "recipe_ingredients": ", ".join(i["raw"] for i in best),
            "ingredients_source": best_source,
        }
        result.update(classify(best))       # 정규화 + 주재료/양념 분리
        return result


class CookTimeExtractor(BaseExtractor):
    """조리 시간·인분 추출."""

    name = "cook_time"

    @staticmethod
    def parse_time(text: str) -> tuple[str, int | None]:
        if not text:
            return "", None
        # '조리시간' 문맥 주변 우선 탐색
        ctx = text
        key = re.search(r"(조리\s*시간|소요\s*시간|총\s*시간)", text)
        if key:
            ctx = text[key.start(): key.start() + 60]

        m = _TIME.search(ctx)
        if not m or not any(m.group(g) for g in ("hour", "min", "min1")):
            return "", None

        minutes = 0
        if m.group("hour"):
            minutes += int(m.group("hour")) * 60
        if m.group("min1") and m.group("min2"):
            minutes += int(m.group("min2"))     # 범위면 상한 채택(안전)
        elif m.group("min"):
            minutes += int(m.group("min"))

        if minutes <= 0 or minutes > 600:
            return "", None
        return f"약 {minutes}분", minutes

    def extract(self, record) -> dict:
        display, minutes, servings = "", None, ""
        for source in S.SOURCE_PRIORITY:
            text = getattr(record, source, "")
            if not display:
                display, minutes = self.parse_time(text)
            if not servings:
                sm = _SERVINGS.search(text or "")
                if sm:
                    servings = f"{sm.group(1).replace(' ', '')}인분"
            if display and servings:
                break
        return {
            "cook_time": display,
            "cook_time_minutes": minutes,
            "servings": servings,
        }


class RecipeFlagExtractor(BaseExtractor):
    """설명란·고정댓글이 레시피 정리본인지 표시."""

    name = "recipe_flags"

    def extract(self, record) -> dict:
        return {
            "desc_has_recipe": looks_like_recipe(getattr(record, "recipe_desc", "")),
            "comment_has_recipe": looks_like_recipe(getattr(record, "recipe_comment", "")),
        }


# 새 추출기를 만들면 여기 등록 → 파이프라인이 순회하며 자동 적용
EXTRACTOR_REGISTRY: list[type[BaseExtractor]] = [
    DishNameExtractor,
    RecipeFlagExtractor,
    IngredientExtractor,
    CookTimeExtractor,
]


def run_all(record):
    """등록된 모든 추출기를 순서대로 적용."""
    for cls in EXTRACTOR_REGISTRY:
        record.update(cls().extract(record))
    return record
