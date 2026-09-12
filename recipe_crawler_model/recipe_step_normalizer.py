"""
레시피 정형화 — 원문(음성/설명란)을 번호 매긴 조리 단계로 변환.

목표 출력 형태:
    1. 양념: 다진 마늘 3큰술, 미림 2큰술, 간장 4큰술, 파 1대 다져서 섞어주기
    2. 후라이팬에 식용유 3큰술 두르고 중강불 유지
    3. 기름이 달궈지면 목살 올려서 한 면당 1분씩 6분 구워주기

영상 원문에는 인사·구독요청·잡담이 많아 그대로 쓸 수 없음.
여기서 사족을 걷어내고 '조리 동작이 있는 문장'만 남겨 단계로 재구성함.

한계(중요):
    구어체 ASR 텍스트를 규칙만으로 완벽히 정형화하는 건 불가능함.
    설명란·고정댓글처럼 이미 번호가 매겨진 원문은 거의 그대로 살릴 수 있지만,
    음성 원문은 문장 분리·동작 판별이 빗나갈 수 있음.
    품질이 중요하면 llm_refine() 로 LLM 후처리를 붙이는 걸 권장함(기본 비활성).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ── 사족 필터 ────────────────────────────────────────────────────
# 조리와 무관한 문장. 하나라도 걸리면 그 문장은 통째로 버림.
_NOISE_PATTERNS = re.compile(
    r"구독|좋아요|알림\s*설정|알람\s*설정|채널|댓글|링크|더보기|"
    r"협찬|광고|제공|쿠폰|할인|구매|공구|판매|주소|인스타|블로그|"
    r"안녕하세요|안녕하십니까|여러분|시청|감사합니다|다음\s*영상|지난\s*영상|"
    r"오늘은\s*$|시작해\s*볼|시작하겠|마무리하겠|이상입니다|"
    r"맛있겠|맛있어요|맛있네|진짜\s*맛|존맛|꿀맛|대박|"
    r"제가\s*좋아하는|개인적으로|사실\s*저는|요즘|날씨"
)

# ── 조리 동작 동사 ───────────────────────────────────────────────
# 이 중 하나라도 있어야 '조리 단계'로 인정함.
_COOK_VERBS = re.compile(
    r"넣|볶|굽|구워|섞|썰|다지|다져|끓|데치|데쳐|튀기|튀겨|부치|부쳐|"
    r"삶|재우|재워|버무리|무치|절이|절여|올리|올려|두르|뿌리|뿌려|"
    r"졸이|졸여|찌|쪄|비비|풀어|갈아|채썰|손질|헹구|씻|불리|불려|"
    r"덮|닫|뒤집|식히|담|따르|붓|따라|우리|익히|익혀"
)

# ── 보존해야 할 조리 정보 ────────────────────────────────────────
# 불세기·시간은 레시피 핵심이라 이게 있으면 사족 필터를 통과시켜 줌.
_KEEP_SIGNALS = re.compile(
    r"약불|중불|강불|중강불|센\s*불|약한\s*불|"
    r"\d+\s*(?:분|초|시간)|\d+\s*(?:g|kg|ml|L|큰술|작은술|컵|개|장|대|쪽|T)"
)

# 이미 번호가 매겨진 줄 (설명란/고정댓글에 흔함)
_NUMBERED = re.compile(r"^\s*(?:\d+[.)]|[①-⑳]|[-•*·▶])\s*(?P<body>.+)$", re.MULTILINE)

# 문장 분리 — 구어체 종결어미 기준
_SENT_SPLIT = re.compile(
    r"(?<=[.!?])\s+|"
    r"(?<=니다)\s+|(?<=세요)\s+|(?<=주세요)\s+|(?<=구요)\s+|(?<=고요)\s+|"
    r"(?<=거든요)\s+|(?<=습니다)\s+|(?<=합니다)\s+|\n+"
)

def _strip_bnida(word: str) -> str:
    """
    'ㅂ니다' 활용 어간 복원 — 종성 ㅂ 을 떼어냄.
    예) 달굽 → 달구, 조립 → 조리, 볶습 → (해당 없음)
    한글 음절은 (초성*21+중성)*28+종성 구조라 종성만 0으로 만들면 됨.
    """
    if not word:
        return word
    code = ord(word[-1])
    if 0xAC00 <= code <= 0xD7A3 and (code - 0xAC00) % 28 == 17:   # 종성 ㅂ = 17
        return word[:-1] + chr(code - 17)
    return word


def _fix_hapnida(s: str) -> str:
    """'~습니다/~ㅂ니다' 종결을 '~기' 형태로 변환."""
    if s.endswith("습니다"):                       # 굽습니다 → 굽기
        return s[:-3] + "기"
    if s.endswith("니다"):                         # 조립니다 → 조리기
        return _strip_bnida(s[:-2]) + "기"
    return s


# 구어체 종결 → 간결한 '~기' 형태로 통일 (위에서부터 먼저 걸리는 하나만 적용)
_ENDING_RULES = [
    # '해주세요' 계열은 '해'를 살려야 자연스러움 (유지해주세요 → 유지하기)
    (re.compile(r"해\s*주세요$|해\s*줍니다$|해\s*주시면\s*됩니다$"), "하기"),
    (re.compile(r"(?:주시면|하시면)\s*됩니다$"), "주기"),
    (re.compile(r"주세요$|줍니다$"), "주기"),
    (re.compile(r"하시면\s*돼요$"), "하기"),
    (re.compile(r"합니다$"), "하기"),
    (re.compile(r"됩니다$"), "되기"),
    (re.compile(r"해요$|할게요$|하죠$"), "하기"),
    (re.compile(r"(?:할|하는)\s*거예요$"), "하기"),
    (re.compile(r"세요$"), "기"),
    (re.compile(r"구요$|고요$"), "고"),
    (re.compile(r"거든요$|는데요$|어요$|아요$|네요$|죠$|요$"), ""),
]

# 군더더기 표현 (문장 안에서 제거)
_FILLER = re.compile(
    r"\b(?:자|이제|그럼|그러면|그리고\s*나서|일단|먼저|우선|"
    r"이렇게|저렇게|요렇게|약간|조금|살짝\s*이제|음|어|아|뭐|"
    r"보시면|보시다시피|아시겠지만|같은\s*경우[는에]?)\b"
)


def _clean_sentence(s: str) -> str:
    """문장 하나를 다듬음 — 군더더기 제거 + 종결어미 정리."""
    s = s.strip(" \t.,!?~…-•*·▶")
    if not s:
        return ""
    s = _FILLER.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # 종결어미를 '~기' 형태로 통일 (가장 먼저 걸리는 규칙 하나만 적용)
    matched = False
    for pat, repl in _ENDING_RULES:
        if pat.search(s):
            s = pat.sub(repl, s).strip()
            matched = True
            break
    if not matched:
        s = _fix_hapnida(s)          # '~습니다/~ㅂ니다' 폴백 처리
    # 문두 접속사 잔재 정리 (그리고/그 다음에 등)
    s = re.sub(r"^(?:그리고|그러고|그\s*다음에?|그\s*담에|그\s*후에?|다음으로)\s*", "", s)
    return s.strip(" \t.,!?~…-")


def _is_cooking_step(s: str) -> bool:
    """조리 단계로 볼 문장인지 판정."""
    if len(s) < 5 or len(s) > 120:          # 너무 짧거나 장황하면 제외
        return False
    # 불세기·시간·계량이 있으면 조리 정보로 보고 통과
    if _KEEP_SIGNALS.search(s):
        return not _NOISE_PATTERNS.search(s)
    # 그 외에는 조리 동사가 있어야 하고 사족이 없어야 함
    return bool(_COOK_VERBS.search(s)) and not _NOISE_PATTERNS.search(s)


def _from_numbered(text: str) -> list[str]:
    """
    이미 번호/글머리표가 있는 원문에서 단계 추출.
    설명란·고정댓글은 대개 이 경로로 깔끔하게 처리됨.
    """
    steps = []
    for m in _NUMBERED.finditer(text):
        body = _clean_sentence(m.group("body"))
        # 번호가 붙어 있어도 재료 나열이나 사족일 수 있어 한 번 더 거름
        if body and not _NOISE_PATTERNS.search(body) and len(body) >= 4:
            steps.append(body)
    return steps


def _from_prose(text: str) -> list[str]:
    """줄글(음성 인식 결과 등)에서 조리 단계 추출."""
    steps = []
    for raw in _SENT_SPLIT.split(text):
        s = _clean_sentence(raw)
        if s and _is_cooking_step(s):
            steps.append(s)
    return steps


def _dedup_steps(steps: list[str]) -> list[str]:
    """
    인접 중복·부분 포함 제거.
    ASR 은 같은 말을 반복하는 경우가 잦아 이 정리가 필요함.
    """
    out: list[str] = []
    for s in steps:
        key = re.sub(r"\s+", "", s)
        if not key:
            continue
        dup = False
        for prev in out[-3:]:                       # 최근 3개와만 비교
            pkey = re.sub(r"\s+", "", prev)
            if key == pkey or key in pkey or pkey in key:
                dup = True
                break
        if not dup:
            out.append(s)
    return out


def normalize_recipe(text: str, max_steps: int = 15) -> list[str]:
    """
    원문 텍스트 → 조리 단계 리스트.

    번호가 매겨진 원문이면 그 구조를 살리고,
    아니면 줄글에서 조리 문장만 골라냄.
    """
    if not text or len(text) < 20:
        return []

    steps = _from_numbered(text)
    # 번호 구조가 빈약하면 줄글 경로로 폴백
    if len(steps) < 2:
        steps = _from_prose(text)

    steps = _dedup_steps(steps)
    return steps[:max_steps]


def format_steps(steps: list[str]) -> str:
    """단계 리스트를 '1. ...\\n2. ...' 문자열로 변환."""
    return "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))


def build_processed_recipe(record: dict) -> dict:
    """
    레코드에서 정형화 레시피를 만들어 반환.

    소스 우선순위는 extract 와 동일하되, 정형화는 '구조가 있는 원문'이
    압도적으로 유리하므로 설명란/고정댓글을 먼저 시도함.

    Returns:
        {"processed_recipe": str, "processed_step_count": int,
         "processed_source": str}
    """
    candidates: list[tuple[str, str]] = []
    if record.get("is_pinned_verified") and record.get("pinned_comment"):
        candidates.append(("pinned_comment", record["pinned_comment"]))
    if record.get("description"):
        candidates.append(("description", record["description"]))
    if record.get("pinned_comment") and not record.get("is_pinned_verified"):
        candidates.append(("pinned_comment_unverified", record["pinned_comment"]))
    if record.get("subtitle_is_manual") and record.get("recipe_subtitle"):
        candidates.append(("subtitle_manual", record["recipe_subtitle"]))
    if record.get("recipe_audio"):
        candidates.append(("audio", record["recipe_audio"]))
    if record.get("recipe_subtitle"):
        candidates.append(("subtitle_auto", record["recipe_subtitle"]))

    best_steps, best_src = [], ""
    for label, text in candidates:
        steps = normalize_recipe(text)
        if len(steps) > len(best_steps):
            best_steps, best_src = steps, label
        if len(best_steps) >= 4:                # 충분하면 조기 종료
            break

    return {
        "processed_recipe": format_steps(best_steps),
        "processed_step_count": len(best_steps),
        "processed_source": best_src,
    }


# ──────────────────────────────────────────────────────────────────
# 선택: LLM 후처리 (기본 비활성)
# ──────────────────────────────────────────────────────────────────
def llm_refine(raw_text: str, dish_name: str = "", api_key: str = "",
               model: str = "claude-sonnet-4-6") -> str:
    """
    규칙 기반 결과가 부족할 때 LLM 으로 다듬는 선택 경로.

    구어체 원문을 단계로 재구성하는 건 규칙만으로 한계가 뚜렷해서,
    품질이 중요하면 이 경로를 쓰는 게 현실적임.
    호출 비용이 있으니 기본은 끄고, 필요할 때만 켜서 사용할 것.

    Returns:
        정형화된 레시피 문자열. 실패 시 빈 문자열.
    """
    if not api_key or not raw_text:
        return ""
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic 미설치 — LLM 후처리 건너뜀")
        return ""

    prompt = (
        f"다음은 유튜브 요리 영상('{dish_name}')의 음성 원문이다.\n"
        "조리 과정만 뽑아 번호 매긴 단계로 정리해라.\n"
        "규칙: 인사·구독요청·잡담 제외. 재료 분량, 불 세기, 시간은 반드시 보존. "
        "각 단계는 한 줄, '~기' 형태로 끝낼 것. 원문에 없는 내용 추가 금지.\n\n"
        f"원문:\n{raw_text[:4000]}"
    )
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model, max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:
        logger.warning("LLM 후처리 실패: %s", e)
        return ""
