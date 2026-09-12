"""
4단계 — 레시피 항목 추출 (요리명 / 재료 / 조리시간).

추출 우선순위 (신뢰도 높은 순):
  1. 고정댓글(업로더 작성 검증분)   — 정제된 레시피가 적혀 있는 경우가 많음
  2. 영상 설명란                    — 실제로는 여기에 레시피 전문이 가장 많음
  3. 자막(수동)                     — 업로더 작성이라 신뢰도 있음
  4. 음성 인식 텍스트               — 항상 존재하지만 구어체라 파싱 난도 높음
요리명은 영상 제목에서도 뽑을 수 있어 별도 경로로 처리함.

어디서 뽑았는지 extraction_source 에 남겨 후단에서 신뢰도 가중이 가능하게 함.
"""

from __future__ import annotations

import json
import re

# ── 단위 사전 ────────────────────────────────────────────────────
# 한국어 레시피에 흔한 계량 단위. 재료 줄 판별의 핵심 신호임.
UNITS = (
    r"g|kg|ml|mL|L|l|cc|" 
    r"큰술|작은술|숟갈|스푼|티스푼|테이블스푼|컵|줌|꼬집|" 
    r"개|알|쪽|장|줄|대|통|봉지|봉|팩|캔|마리|공기|인분|모|단|손|" 
    r"T|t|Ts|ts"
)

# "간장 2큰술", "돼지고기 목살 300g", "마늘 3개" 같은 줄 매칭
_ING_LINE = re.compile(
    rf"^[\s\-•*·▶▪◾>]*"                      # 글머리 기호 허용
    rf"(?P<name>[가-힣A-Za-z][^:：\n]{{0,30}}?)\s*"   # 재료명
    rf"(?P<amount>\d+(?:[./~-]\d+)?)\s*"      # 수량(분수·범위 허용)
    rf"(?P<unit>{UNITS})\b",
    re.MULTILINE,
)

# 음성/자막용 — 줄 단위가 아닌 연속 문장에서 "재료명 수량단위" 추출.
# 줄 앵커(^)가 없어 문장 중간의 재료도 잡히지만, 그만큼 오탐이 늘어
# 재료명을 한글 1~10자로 좁혀 보수적으로 매칭함.
_ING_INLINE = re.compile(
    rf"(?P<name>[가-힣]{{1,10}})\s*"
    rf"(?P<amount>\d+(?:[./~-]\d+)?)\s*"
    rf"(?P<unit>{UNITS})\b",
)

# 재료명에 오면 안 되는 표현 (감탄·잡담·조리도구 등)
_NOT_INGREDIENT = re.compile(
    r"맛있|진짜|정말|너무|완전|대박|좋아|저는|제가|여러분|오늘|영상|"
    r"구독|좋아요|댓글|채널|"
    r"후라이팬|프라이팬|팬에|냄비|그릇|접시|볼에|도마|칼|불로|불에"
)

# 재료 섹션 헤더 — 이 아래 줄들을 재료 후보로 봄
_ING_HEADER = re.compile(r"(재\s*료|준비\s*물|材料|ingredients?)\s*[:：]?", re.IGNORECASE)

# 조리시간 — "약 15분", "10~15분", "1시간 30분"
_TIME = re.compile(
    r"(?:조리\s*시간|소요\s*시간|총\s*시간)?\s*"
    r"(?:약\s*)?"
    r"(?:(?P<hour>\d+)\s*시간)?\s*"
    r"(?:(?P<min1>\d+)\s*[~-]\s*(?P<min2>\d+)|(?P<min>\d+))?\s*분",
)

# 인분 — "2인분", "1~2인분"
_SERV = re.compile(r"(\d+(?:\s*[~-]\s*\d+)?)\s*인분")

# 제목에서 걷어낼 잡음 (대괄호·해시태그·이모지성 기호)
_TITLE_NOISE = re.compile(
    r"\[[^\]]*\]|\([^)]*\)|#\S+|[|｜/]+|"
    r"(?:구독|좋아요|레시피|만들기|만드는\s*법|초간단|간단|자취|혼밥|"
    r"asmr|ASMR|shorts|Shorts|SHORTS)|"
    r"\d+\s*(?:분|초)\s*(?:완성|컷|요리)?|"        # "10분 완성" 같은 홍보 문구
    r"[Ee][Pp]\.?\s*\d+",                        # 회차 표기
)


def extract_dish_name(title: str, text: str = "") -> str:
    """
    요리 이름 추출. 영상 제목 기반이 가장 안정적임.

    제목의 대괄호·해시태그·홍보문구를 걷어내고 남은 핵심 명사구를 사용함.
    """
    if not title:
        return ""
    name = _TITLE_NOISE.sub(" ", title)
    name = re.sub(r"[^\w가-힣\s]", " ", name)     # 잔여 특수문자 제거
    name = re.sub(r"\s+", " ", name).strip()
    # 너무 길면 앞 어절만 (제목에 설명이 길게 붙는 경우 대비)
    parts = name.split()
    if len(parts) > 6:
        name = " ".join(parts[:6])
    return name


def extract_ingredients(text: str) -> list[dict]:
    """
    텍스트에서 재료 목록 추출.

    재료 섹션 헤더가 있으면 그 이후 구간을 우선 파싱하고,
    없으면 전체에서 '수량+단위' 패턴을 훑음.

    Returns:
        [{"name": "간장", "amount": "2", "unit": "큰술", "raw": "간장 2큰술"}, ...]
    """
    if not text:
        return []

    # 섹션 헤더가 있으면 그 뒤 600자를 우선 대상으로 삼음
    m = _ING_HEADER.search(text)
    scope = text[m.end(): m.end() + 600] if m else text

    # 줄 기반(정형 목록)과 연속문장(구어체) 둘 다 시도 후 많이 뽑힌 쪽 채택
    line_hits = list(_ING_LINE.finditer(scope))
    inline_hits = list(_ING_INLINE.finditer(scope))
    matches = line_hits if len(line_hits) >= len(inline_hits) else inline_hits

    found, seen = [], set()
    for mm in matches:
        name = mm.group("name").strip(" -•*·▶▪◾>\t")
        # 문장부호 뒤는 앞 문장 잔재이므로 잘라냄
        name = re.split(r"[.!?…]", name)[-1]
        # 구어체 접속/서술 꼬리 제거 ("넣고 밥" → "밥")
        name = re.sub(
            r"^.*?(?:넣고|넣어|하고|그리고|그\s*다음에?|이제|일단|먼저|"
            r"준비한|썰어둔|다져둔)\s+", "", name).strip()
        # 재료명은 보통 1~2어절 — 그보다 길면 뒤쪽 2어절만 사용
        toks = name.split()
        if len(toks) > 2:
            name = " ".join(toks[-2:])
        # 사족 표현이 남아 있으면 재료가 아님
        if _NOT_INGREDIENT.search(name):
            continue
        # 재료명이 비었거나 숫자만이면 스킵
        if not name or name.isdigit() or len(name) > 25:
            continue
        key = name.replace(" ", "")
        if key in seen:                              # 같은 재료 중복 방지
            continue
        seen.add(key)
        found.append({
            "name": name,
            "amount": mm.group("amount"),
            "unit": mm.group("unit"),
            "raw": mm.group(0).strip(" -•*·▶▪◾>\t"),
        })
        if len(found) >= 30:                         # 비정상 폭주 방지
            break
    return found


def extract_cook_time(text: str) -> tuple[str, int | None]:
    """
    조리 시간 추출.

    Returns:
        (표시용 문자열 "약 15분", 분 단위 정수 15)
        못 찾으면 ("", None)
    """
    if not text:
        return "", None

    # '조리시간' 문맥 주변을 우선 탐색 → 없으면 전체
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
        # 범위면 상한 사용 (넉넉하게 잡는 편이 실사용에 안전)
        minutes += int(m.group("min2"))
    elif m.group("min"):
        minutes += int(m.group("min"))

    if minutes <= 0 or minutes > 600:                # 비현실적 값 배제
        return "", None
    return f"약 {minutes}분", minutes


def extract_servings(text: str) -> str:
    """인분 정보 추출 (없으면 빈 문자열)."""
    m = _SERV.search(text or "")
    return f"{m.group(1).replace(' ', '')}인분" if m else ""


def extract_recipe(record: dict) -> dict:
    """
    레코드 하나에서 레시피 항목을 추출해 채워 넣음.

    우선순위대로 소스를 훑되, 재료를 가장 많이 뽑아낸 소스를 채택함
    (앞선 소스에서 하나도 못 뽑으면 다음 소스로 자연스럽게 넘어감).
    """
    # (라벨, 텍스트) 우선순위 목록
    sources: list[tuple[str, str]] = []
    if record.get("is_pinned_verified") and record.get("pinned_comment"):
        sources.append(("pinned_comment", record["pinned_comment"]))
    if record.get("description"):
        sources.append(("description", record["description"]))
    if record.get("pinned_comment") and not record.get("is_pinned_verified"):
        sources.append(("pinned_comment_unverified", record["pinned_comment"]))
    if record.get("subtitle_is_manual") and record.get("recipe_subtitle"):
        sources.append(("subtitle_manual", record["recipe_subtitle"]))
    if record.get("recipe_audio"):
        sources.append(("audio", record["recipe_audio"]))
    if record.get("recipe_subtitle"):
        sources.append(("subtitle_auto", record["recipe_subtitle"]))

    best_ing, best_label = [], ""
    for label, text in sources:
        ing = extract_ingredients(text)
        if len(ing) > len(best_ing):
            best_ing, best_label = ing, label
        if len(best_ing) >= 4:            # 충분히 뽑았으면 조기 종료
            break

    # 조리시간·인분은 소스 순서대로 처음 발견된 값 사용
    time_disp, time_min, servings, time_label = "", None, "", ""
    for label, text in sources:
        if not time_disp:
            time_disp, time_min = extract_cook_time(text)
            if time_disp:
                time_label = label
        if not servings:
            servings = extract_servings(text)
        if time_disp and servings:
            break

    # 출처 표기 — 재료/시간이 서로 다른 소스일 수 있어 함께 기록
    src_parts = []
    if best_label:
        src_parts.append(f"ingredients:{best_label}")
    if time_label:
        src_parts.append(f"time:{time_label}")
    src_parts.append("dish:title")

    return {
        "dish_name": extract_dish_name(record.get("video_title", "")),
        "ingredients": ", ".join(i["raw"] for i in best_ing),
        "ingredients_json": json.dumps(best_ing, ensure_ascii=False),
        "cook_time_display": time_disp,
        "cook_time_min": time_min,
        "servings": servings,
        "extraction_source": " | ".join(src_parts),
    }
