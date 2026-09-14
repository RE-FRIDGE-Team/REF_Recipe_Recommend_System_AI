"""
ingredient_normalizer.py — 재료명 정규화 및 주재료/양념 분류.

이 모듈이 크롤러와 추천 시스템을 잇는 핵심임.

왜 필요한가:
    추천 로직은 '냉장고 재고 ∩ 레시피 재료' 를 계산함. 그런데
      - 크롤러가 뽑은 이름: "돼지고기 목살", "다진 마늘", "대파 1대"
      - 냉장고 재고 이름  : "목살", "마늘", "대파"  (PGIN 키)
    표기가 달라 문자열 그대로는 매칭이 안 됨. 수식어(다진/국내산/냉동)를
    걷어내고 핵심 명사만 남겨야 매칭률이 나옴.

    또 하나: 간장·소금·설탕 같은 양념을 매칭 분모에 넣으면 거의 모든
    레시피가 '재료 부족' 으로 떨어짐. 양념은 상비 품목이고 유통기한 압박도
    적으므로 주재료와 분리해야 추천이 실용적임.

매칭 키:
    재고 대조 키는 PGIN(processed_grocery_item_name, 491종)임.
    GIN(grocery_item_name) 은 "란 → 계란" 같은 부정확한 표기가 많아
    매핑 소스로 쓰지 않음. 대신 PGIN 에 딸린 카테고리(대분류/중분류)를
    함께 싣고, 정확 매칭이 안 될 때 카테고리 단위로 대체 가능성을 판정함.

    카테고리의 주 용도는 '대체' 가 아니라 '해석' 임.
    레시피가 "돼지고기 목살" 이라고 써도 재고 키는 "목살" 이므로,
    카테고리명(돼지고기)을 단서로 같은 PGIN 에 모아줌.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 양념·조미료 사전 ─────────────────────────────────────────────
# 상비 품목이라 매칭 분모에서 제외할 대상.
# 확장: 새 양념이 보이면 여기 추가하면 전 파이프라인에 반영됨.
SEASONINGS: set[str] = {
    # 장류·소스
    "간장", "진간장", "국간장", "양조간장", "된장", "고추장", "쌈장", "춘장",
    "굴소스", "피시소스", "액젓", "멸치액젓", "까나리액젓",
    "케찹", "케첩", "마요네즈", "머스타드", "머스터드", "돈까스소스",
    "칠리소스", "스리라차", "타바스코", "우스터소스", "데리야끼소스",
    # 기본 조미
    "소금", "설탕", "후추", "후춧가루", "흑후추", "백설탕", "황설탕",
    "미원", "다시다", "치킨스톡", "맛소금", "조미료", "연두",
    # 산·주류 조미
    "식초", "사과식초", "현미식초", "발사믹", "맛술", "미림", "청주",
    "레몬즙", "매실청", "올리고당", "물엿", "꿀",
    # 기름
    "식용유", "올리브유", "참기름", "들기름", "포도씨유", "카놀라유",
    # 가루·향신료
    "고춧가루", "고추가루", "깨", "통깨", "참깨", "밀가루", "전분", "감자전분",
    "옥수수전분", "녹말", "베이킹파우더", "카레가루", "카레", "강황",
    "파프리카가루", "오레가노", "바질", "로즈마리", "월계수잎", "계피",
    "생강", "생강가루", "다진마늘", "마늘가루", "양파가루", "허브",
    # 기타
    "물", "육수", "다시마육수", "멸치육수",
}

# ── 수식어 (제거 대상) ───────────────────────────────────────────
MODIFIERS: list[str] = [
    # 손질 상태
    "다진", "썬", "채썬", "채썰은", "슬라이스", "깍둑썬", "편썬", "어슷썬",
    "손질한", "손질된", "삶은", "데친", "볶은", "구운", "튀긴", "찐",
    "불린", "절인", "숙성", "해동한", "냉동", "냉장", "생",
    # 품질·산지
    "국내산", "수입산", "유기농", "무농약", "친환경", "신선한", "싱싱한",
    "프리미엄", "햇",
    # 크기·수량 수식
    "큰", "작은", "중간", "적당량", "약간", "깐", "손질",
    # 형태
    "반", "낱개",
]

# 단일 글자 수식어는 반드시 공백이 뒤따를 때만 제거함.
# 공백 없이 붙으면 재료명 자체일 수 있기 때문 ("대파"의 "대", "생강"의 "생").
_MULTI_MOD = [m for m in MODIFIERS if len(m) >= 2]
_SINGLE_MOD = [m for m in MODIFIERS if len(m) == 1]
_MODIFIER_RE = re.compile(
    r"^(?:(?:" + "|".join(map(re.escape, _MULTI_MOD)) + r")\s*"
    r"|(?:" + "|".join(map(re.escape, _SINGLE_MOD)) + r")\s+)"
)

_PAREN_RE = re.compile(r"[\(（][^)）]*[\)）]")
_TRAILING_RE = re.compile(r"[\s,./·]+$")


# ══════════════════════════════════════════════════════════════════
# PGIN 어휘 + 카테고리
# ══════════════════════════════════════════════════════════════════
# 재고 대조용 매칭 키. 비어 있으면 스냅을 건너뛰고 정규화만 수행함.
PGIN_VOCAB: set[str] = set()

# PGIN → (대분류, 중분류).
PGIN_CATEGORY: dict[str, tuple[str, str]] = {}

# 공백 제거 형태 → 원본 PGIN. "닭 가슴살" 처럼 띄어쓰기가 다른 표기를 흡수함.
_PGIN_COMPACT: dict[str, str] = {}

# 중분류 → 그 카테고리에 속한 PGIN 목록. 카테고리 보조 해석에 씀.
_PGIN_BY_MEDIUM: dict[str, list[str]] = {}

# 카테고리 이름 집합 (대분류·중분류). 재료명이 카테고리 자체인 경우 판별용.
CATEGORY_NAMES: set[str] = set()


def _compact(text: str) -> str:
    """공백 제거 — 띄어쓰기 차이를 무시하고 비교하기 위함."""
    return re.sub(r"\s+", "", text or "")


def _build_index() -> None:
    """PGIN 어휘·카테고리로부터 보조 인덱스를 구성함."""
    global _PGIN_COMPACT, _PGIN_BY_MEDIUM, CATEGORY_NAMES
    _PGIN_COMPACT = {_compact(g): g for g in PGIN_VOCAB}
    by_medium: dict[str, list[str]] = {}
    names: set[str] = set()
    for pgin, (large, medium) in PGIN_CATEGORY.items():
        if medium:
            by_medium.setdefault(medium, []).append(pgin)
            names.add(medium)
        if large:
            names.add(large)
    _PGIN_BY_MEDIUM = by_medium
    CATEGORY_NAMES = names


def load_pgin_vocab(
    path: str | Path,
    pgin_column: str = "processed_grocery_item_name",
    large_column: str = "category_large",
    medium_column: str = "category_medium",
) -> set[str]:
    """
    재고 대조용 PGIN 어휘와 카테고리를 불러와 전역에 등록함.

    지원 형식:
        - CSV  : pgin_column + 카테고리 컬럼을 읽음
                 (인식 데이터셋 CSV 를 그대로 주면 됨 — 가장 권장)
        - JSON 배열 : ["계란", "양파", ...]                    → 어휘만
        - JSON 객체 : {"목살": ["육류/계란", "돼지고기"], ...}  → 어휘 + 카테고리
        - 텍스트    : 줄바꿈 구분                              → 어휘만

    카테고리가 함께 로드되면 추천 쪽에서 '유사 재료 대체' 판정이 가능해짐.
    """
    global PGIN_VOCAB, PGIN_CATEGORY
    p = Path(path)
    if not p.exists():
        logger.warning("PGIN 어휘 파일 없음: %s (정규화만 수행)", p)
        return PGIN_VOCAB

    try:
        suffix = p.suffix.lower()

        if suffix == ".csv":
            import csv
            vocab: set[str] = set()
            cats: dict[str, tuple[str, str]] = {}
            with open(p, encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                fields = reader.fieldnames or []
                if pgin_column not in fields:
                    logger.warning("CSV 에 %s 컬럼 없음: %s", pgin_column, p)
                    return PGIN_VOCAB
                has_cat = large_column in fields and medium_column in fields
                for row in reader:
                    pgin = (row.get(pgin_column) or "").strip()
                    if not pgin:
                        continue
                    vocab.add(pgin)
                    # 같은 PGIN 은 카테고리가 일관되므로 첫 값만 채택
                    if has_cat and pgin not in cats:
                        large = (row.get(large_column) or "").strip()
                        medium = (row.get(medium_column) or "").strip()
                        if large or medium:
                            cats[pgin] = (large, medium)
            PGIN_VOCAB, PGIN_CATEGORY = vocab, cats
            logger.info("PGIN 어휘 %d개 / 카테고리 %d개 로드", len(vocab), len(cats))

        elif suffix == ".json":
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                PGIN_VOCAB = {str(k).strip() for k in data if str(k).strip()}
                PGIN_CATEGORY = {
                    str(k).strip(): (str(v[0]), str(v[1]))
                    for k, v in data.items()
                    if isinstance(v, (list, tuple)) and len(v) >= 2
                }
                logger.info("PGIN 어휘 %d개 / 카테고리 %d개 로드",
                            len(PGIN_VOCAB), len(PGIN_CATEGORY))
            else:
                PGIN_VOCAB = {str(w).strip() for w in data if str(w).strip()}
                logger.info("PGIN 어휘 %d개 로드", len(PGIN_VOCAB))

        else:
            words = p.read_text(encoding="utf-8").splitlines()
            PGIN_VOCAB = {w.strip() for w in words if w.strip()}
            logger.info("PGIN 어휘 %d개 로드", len(PGIN_VOCAB))

    except Exception as e:
        logger.warning("PGIN 어휘 로드 실패: %s", e)

    _build_index()
    return PGIN_VOCAB


def _clean_surface(raw: str) -> str:
    """표층 정리 — 괄호·수식어·후행 기호 제거 (PGIN 스냅 이전 단계)."""
    if not raw:
        return ""
    name = _PAREN_RE.sub(" ", str(raw))
    name = re.sub(r"\s+", " ", name).strip()
    # 수식어가 중첩될 수 있어 반복 제거 ("국내산 다진 마늘")
    for _ in range(3):
        new = _MODIFIER_RE.sub("", name).strip()
        if new == name:
            break
        name = new
    return _TRAILING_RE.sub("", name).strip()


def resolve(raw: str) -> tuple[str, str]:
    """
    재료명을 PGIN 키로 해석함.

    레시피는 "돼지고기 목살" 처럼 카테고리명을 앞에 붙여 쓰는 경우가 많은데,
    재고의 PGIN 은 "목살" 이라 표기가 어긋남. 카테고리를 단서로 삼아
    이런 표기를 같은 PGIN 으로 모아주는 게 이 함수의 역할임.

    해석 순서:
        1. exact            — PGIN 직접 일치 (띄어쓰기 차이 무시)
        2. category_assisted— 이름에 카테고리명이 섞인 경우, 그 카테고리에
                              속한 PGIN 중에서 나머지 부분과 맞는 것을 채택
                              ("돼지고기 목살" → 중분류 돼지고기 안의 "목살")
                              ("소고기 등심"   → 중분류 소고기 안의 "소 등심")
        3. partial          — 이름에 포함된 가장 긴 PGIN
        4. category         — 이름 자체가 카테고리명 ("돼지고기", "채소")
                              특정 품목이 아니라 범주를 가리키는 경우
        5. unmatched        — PGIN 에 없는 재료 (어휘 보강 후보)

    Returns:
        (해석된 이름, 해석 방식)
    """
    name = _clean_surface(raw)
    if not name:
        return "", "unmatched"
    if not PGIN_VOCAB:
        return name, "unmatched"

    compact = _compact(name)

    # 1) 직접 일치 (띄어쓰기 무시 — "닭 가슴살" → "닭가슴살")
    if compact in _PGIN_COMPACT:
        return _PGIN_COMPACT[compact], "exact"

    # 2) 카테고리 보조 해석
    #    이름 안에 카테고리명이 있으면, 그 카테고리 소속 PGIN 으로 범위를 좁혀
    #    나머지 부분과 맞춰봄. 범위가 좁아 오탐이 적음.
    for cat in CATEGORY_NAMES:
        if not cat or _compact(cat) not in compact:
            continue
        remainder = _compact(name.replace(cat, " "))
        if not remainder:
            continue
        candidates = _PGIN_BY_MEDIUM.get(cat)
        if candidates is None:
            # 대분류로 걸린 경우 — 해당 대분류의 모든 PGIN 을 후보로
            candidates = [g for g, (lg, _m) in PGIN_CATEGORY.items() if lg == cat]
        hits = []
        for g in candidates:
            gc = _compact(g)
            # 남은 표현이 PGIN 에 포함되거나 그 반대면 후보
            # ("등심" ⊂ "소등심", "목살" == "목살")
            if remainder in gc or gc in remainder:
                hits.append(g)
        if hits:
            return max(hits, key=len), "category_assisted"

    # 3) 부분 일치 — 이름에 포함된 가장 긴 PGIN
    hits = [g for g in PGIN_VOCAB if g and _compact(g) in compact]
    if hits:
        return max(hits, key=len), "partial"

    # 4) 이름 자체가 카테고리 ("돼지고기", "채소")
    #    특정 품목이 아니라 범주를 가리킴 — 추천 쪽에서 범주 단위로 처리 가능
    if name in CATEGORY_NAMES:
        return name, "category"

    # 5) 미등재
    return name, "unmatched"


def normalize_name(raw: str) -> str:
    """resolve() 의 이름만 반환하는 단축 함수."""
    return resolve(raw)[0]


def get_category(pgin: str) -> tuple[str, str]:
    """PGIN 의 (대분류, 중분류) 반환. 미등재면 빈 문자열."""
    return PGIN_CATEGORY.get(pgin, ("", ""))


def is_seasoning(name: str) -> bool:
    """
    양념·조미료 여부 판정.

    사전 직접 일치 또는 사전 항목 포함 시 양념으로 봄.
    PGIN 카테고리가 '양념/소스' 계열이면 그것도 근거로 씀.
    """
    if not name:
        return False
    compact = name.replace(" ", "")
    if compact in SEASONINGS:
        return True
    # PGIN 카테고리 기반 판정 (사전에 없는 양념도 잡힘)
    _, medium = get_category(name)
    if medium and ("양념" in medium or "소스" in medium or "조미" in medium):
        return True
    return any(s in compact for s in SEASONINGS if len(s) >= 2)


def classify(ingredients: list[dict]) -> dict:
    """
    재료 리스트를 정규화하고 주재료/양념으로 분류함.

    Args:
        ingredients: [{"name","amount","unit","raw"}, ...] (extractors 출력)

    Returns:
        {
          "ingredients_json": JSON 문자열(role·카테고리 포함),
          "main_ingredients": "목살|마늘|대파",
          "seasoning_ingredients": "간장|미림",
          "main_ingredient_count": 3,
          "main_ingredient_mediums": "돼지고기|채소|채소",
          "unresolved_ingredients": "",   # PGIN 에 없던 재료 (어휘 보강 후보)
        }

    role 이 추천 로직의 분기점이 됨:
        main      → 냉장고 재고와 매칭할 대상(분모)
        seasoning → 상비 가정, 매칭 분모에서 제외

    main_ingredient_mediums 는 재료가 속한 중분류. 해석 근거 확인용이며,
    유사 재료 대체 판정은 추천 시스템 쪽 과제로 남겨둠(현 단계에서는 미적용).
    """
    enriched: list[dict] = []
    mains: list[str] = []
    main_mediums: list[str] = []
    seasonings: list[str] = []

    unresolved: list[str] = []

    for item in ingredients:
        norm, match_type = resolve(item.get("name", ""))
        if not norm:
            continue
        large, medium = get_category(norm)
        role = "seasoning" if is_seasoning(norm) else "main"
        enriched.append({
            "name": norm,                       # PGIN 키 (매칭용)
            "raw_name": item.get("name", ""),   # 원문 표기 (사람 확인용)
            "amount": item.get("amount", ""),
            "unit": item.get("unit", ""),
            "role": role,
            "category_large": large,
            "category_medium": medium,
            "match_type": match_type,           # exact/category_assisted/partial/category/unmatched
        })
        if match_type == "unmatched":
            unresolved.append(norm)             # 어휘 보강 후보로 남김
        if role == "seasoning":
            seasonings.append(norm)
        else:
            mains.append(norm)
            main_mediums.append(medium)

    # 중복 제거(순서 보존) — mediums 는 mains 와 인덱스를 맞춰야 하므로 함께 처리
    seen: set[str] = set()
    uniq_mains: list[str] = []
    uniq_mediums: list[str] = []
    for name, medium in zip(mains, main_mediums):
        if name in seen:
            continue
        seen.add(name)
        uniq_mains.append(name)
        uniq_mediums.append(medium)

    return {
        "ingredients_json": json.dumps(enriched, ensure_ascii=False),
        "main_ingredients": "|".join(uniq_mains),
        "seasoning_ingredients": "|".join(dict.fromkeys(seasonings)),
        "main_ingredient_count": len(uniq_mains),
        "main_ingredient_mediums": "|".join(uniq_mediums),
        "unresolved_ingredients": "|".join(dict.fromkeys(unresolved)),
    }
