"""
processors.py — 원문 → 정제 레시피 변환 (recipe_processed 컬럼).

원문에는 인사·구독요청·잡담이 섞여 있어 그대로 쓸 수 없음.
조리 동작이 있는 문장만 남겨 번호 단계로 재구성함.

목표 출력:
    1. 양념: 다진 마늘 3큰술, 미림 2큰술, 간장 4큰술 섞어주기
    2. 후라이팬에 식용유 3큰술 두르고 중강불 유지하기
    3. 기름이 달궈지면 목살 올려서 한 면당 1분씩 6분 구워주기

확장 구조:
    BaseProcessor 를 상속해 정제 방식을 갈아끼울 수 있음.
      RuleBasedProcessor — 규칙 기반(기본, 무료, 오프라인)
      LLMProcessor       — LLM 호출(품질 우위, 비용 발생)
    PROCESSOR_REGISTRY 에 등록 후 --processor 옵션으로 선택함.

한계(중요):
    구어체 ASR 텍스트를 규칙만으로 완벽히 정형화하는 건 불가능함.
    번호가 이미 매겨진 원문(설명란·고정댓글)은 거의 그대로 살릴 수 있지만,
    음성 원문은 문장 분리·동작 판별이 빗나갈 수 있음.
    대량 처리 시 규칙 기반으로 1차 정제하고, 품질이 중요한 건만
    LLM 으로 재처리하는 혼합 운영을 권장함.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod

from . import settings as S

logger = logging.getLogger(__name__)

# ── 사족 필터 ────────────────────────────────────────────────────
_NOISE = re.compile(
    r"구독|좋아요|알림\s*설정|채널|댓글|링크|더보기|"
    r"협찬|광고|제공|쿠폰|할인|구매|공구|판매|인스타|블로그|"
    r"안녕하세요|여러분|시청|감사합니다|다음\s*영상|지난\s*영상|"
    r"시작해\s*볼|시작하겠|마무리하겠|이상입니다|"
    r"맛있겠|맛있어요|맛있네|존맛|꿀맛|대박|"
    r"제가\s*좋아하는|개인적으로|요즘|날씨"
)

# ── 조리 동작 동사 ───────────────────────────────────────────────
_COOK_VERBS = re.compile(
    r"넣|볶|굽|구워|섞|썰|다지|다져|끓|데치|데쳐|튀기|튀겨|부치|부쳐|"
    r"삶|재우|재워|버무리|무치|절이|절여|올리|올려|두르|뿌리|뿌려|"
    r"졸이|졸여|찌|쪄|비비|풀어|갈아|채썰|손질|헹구|씻|불리|불려|"
    r"덮|닫|뒤집|식히|담|따르|붓|익히|익혀"
)

# ── 보존할 조리 정보 (불 세기·시간·계량) ─────────────────────────
_KEEP_SIGNALS = re.compile(
    r"약불|중불|강불|중강불|센\s*불|약한\s*불|"
    r"\d+\s*(?:분|초|시간)|\d+\s*(?:g|kg|ml|L|큰술|작은술|컵|개|장|대|쪽|T)"
)

_NUMBERED = re.compile(r"^\s*(?:\d+[.)]|[①-⑳]|[-•*·▶])\s*(?P<body>.+)$", re.M)

# 재료 섹션 헤더 — 이 뒤 목록은 조리 단계가 아니라 재료 나열이므로 잘라냄
_ING_SECTION = re.compile(r"(?:재\s*료|준비\s*물|ingredients?)\s*[:：]?", re.I)
# 조리 단계 헤더 — 여기서부터가 실제 단계
_STEP_SECTION = re.compile(
    r"(?:만드는\s*법|조리\s*법|조리\s*순서|레시피|만들기|순서|steps?|"
    r"directions?|how\s*to)\s*[:：]?", re.I)
# '재료명 수량단위' 만으로 이뤄진 줄 (조리 동작이 없음)
_INGREDIENT_ONLY = re.compile(
    r"^[가-힣A-Za-z\s]{1,20}\s*\d+(?:[./~-]\d+)?\s*"
    r"(?:g|kg|ml|mL|L|cc|큰술|작은술|스푼|컵|줌|꼬집|개|알|쪽|장|줄|대|통|"
    r"봉지|봉|팩|캔|마리|공기|인분|모|단|T|t)\s*$"
)

_SENT_SPLIT = re.compile(
    r"(?<=[.!?])\s+|(?<=니다)\s+|(?<=세요)\s+|(?<=주세요)\s+|"
    r"(?<=구요)\s+|(?<=고요)\s+|(?<=거든요)\s+|\n+"
)

_FILLER = re.compile(
    r"\b(?:자|이제|그럼|그러면|그리고\s*나서|일단|먼저|우선|"
    r"이렇게|저렇게|약간|음|어|아|뭐|보시면|보시다시피)\b"
)

# 구어체 종결 → '~기' 형태 통일 (먼저 걸리는 규칙 하나만 적용)
_ENDING_RULES = [
    (re.compile(r"해\s*주세요$|해\s*줍니다$|해\s*주시면\s*됩니다$"), "하기"),
    (re.compile(r"(?:주시면|하시면)\s*됩니다$"), "주기"),
    (re.compile(r"주세요$|줍니다$"), "주기"),
    (re.compile(r"합니다$"), "하기"),
    (re.compile(r"됩니다$"), "되기"),
    (re.compile(r"해요$|할게요$|하죠$"), "하기"),
    (re.compile(r"세요$"), "기"),
    (re.compile(r"구요$|고요$"), "고"),
    (re.compile(r"거든요$|는데요$|어요$|아요$|네요$|죠$|요$"), ""),
]


def _strip_bnida(word: str) -> str:
    """
    'ㅂ니다' 활용 어간 복원 — 종성 ㅂ 제거.
    예) 달굽 → 달구, 조립 → 조리
    한글 음절은 (초성*21+중성)*28+종성 구조라 종성만 0으로 만들면 됨.
    """
    if not word:
        return word
    code = ord(word[-1])
    if 0xAC00 <= code <= 0xD7A3 and (code - 0xAC00) % 28 == 17:   # 종성 ㅂ
        return word[:-1] + chr(code - 17)
    return word


def _fix_hapnida(s: str) -> str:
    """'~습니다/~ㅂ니다' 종결을 '~기' 형태로."""
    if s.endswith("습니다"):            # 굽습니다 → 굽기
        return s[:-3] + "기"
    if s.endswith("니다"):              # 조립니다 → 조리기
        return _strip_bnida(s[:-2]) + "기"
    return s


class BaseProcessor(ABC):
    """정제기 공통 인터페이스."""

    name: str = "base"

    @abstractmethod
    def process(self, record) -> dict:
        """recipe_processed / processed_source / processed_by 반환."""
        raise NotImplementedError


class RuleBasedProcessor(BaseProcessor):
    """규칙 기반 정제 (기본값, 오프라인·무료)."""

    name = "rule"

    def __init__(self, max_steps: int = 15) -> None:
        self.max_steps = max_steps

    # ── 문장 정리 ────────────────────────────────────────────────
    def _clean(self, s: str) -> str:
        s = s.strip(" \t.,!?~…-•*·▶")
        if not s:
            return ""
        s = _FILLER.sub(" ", s)
        s = re.sub(r"\s+", " ", s).strip()

        matched = False
        for pat, repl in _ENDING_RULES:
            if pat.search(s):
                s = pat.sub(repl, s).strip()
                matched = True
                break
        if not matched:
            s = _fix_hapnida(s)

        # 문두 접속사 잔재 제거
        s = re.sub(r"^(?:그리고|그러고|그\s*다음에?|그\s*담에|그\s*후에?|다음으로)\s*", "", s)
        return s.strip(" \t.,!?~…-")

    def _is_step(self, s: str) -> bool:
        """조리 단계로 볼 문장인지 판정."""
        if len(s) < 5 or len(s) > 120:
            return False
        if _KEEP_SIGNALS.search(s):          # 불 세기·시간·계량은 조리 정보
            return not _NOISE.search(s)
        return bool(_COOK_VERBS.search(s)) and not _NOISE.search(s)

    def _from_numbered(self, text: str) -> list[str]:
        """번호·글머리표가 있는 원문에서 단계 추출 (설명란·댓글에 흔함)."""
        steps = []
        for m in _NUMBERED.finditer(text):
            raw_body = m.group("body").strip()
            if _INGREDIENT_ONLY.match(raw_body):   # 재료 나열 줄은 단계가 아님
                continue
            body = self._clean(raw_body)
            if body and not _NOISE.search(body) and len(body) >= 4:
                steps.append(body)
        return steps

    def _from_prose(self, text: str) -> list[str]:
        """줄글(음성 인식 결과)에서 조리 문장만 추출."""
        steps = []
        for raw in _SENT_SPLIT.split(text):
            s = self._clean(raw)
            if s and self._is_step(s):
                steps.append(s)
        return steps

    @staticmethod
    def _dedup(steps: list[str]) -> list[str]:
        """인접 중복·부분 포함 제거 (ASR 은 같은 말을 반복하는 경우가 잦음)."""
        out: list[str] = []
        for s in steps:
            key = re.sub(r"\s+", "", s)
            if not key:
                continue
            if any(key == re.sub(r"\s+", "", p) or key in re.sub(r"\s+", "", p)
                   or re.sub(r"\s+", "", p) in key for p in out[-3:]):
                continue
            out.append(s)
        return out

    @staticmethod
    def _strip_ingredient_section(text: str) -> str:
        """
        재료 나열 구간을 잘라내고 조리 단계 구간만 남김.

        설명란이 '[재료] ... [만드는 법] ...' 구조인 경우가 많은데,
        재료 목록도 글머리표가 붙어 있어 그대로 두면 조리 단계로 오인됨.
        """
        m_step = _STEP_SECTION.search(text)
        if m_step:
            return text[m_step.end():]          # 조리법 헤더 이후만 사용
        m_ing = _ING_SECTION.search(text)
        if m_ing:
            # 조리법 헤더가 없으면 재료 헤더 '이전' 구간이라도 살림
            before = text[:m_ing.start()]
            return before if len(before) > 40 else text
        return text

    def normalize(self, text: str) -> list[str]:
        """원문 → 조리 단계 리스트."""
        if not text or len(text) < 20:
            return []
        text = self._strip_ingredient_section(text)
        steps = self._from_numbered(text)
        if len(steps) < 2:                   # 번호 구조가 빈약하면 줄글 경로
            steps = self._from_prose(text)
        return self._dedup(steps)[:self.max_steps]

    def process(self, record) -> dict:
        best: list[str] = []
        best_source = ""
        for source in S.SOURCE_PRIORITY:
            steps = self.normalize(getattr(record, source, ""))
            if len(steps) > len(best):
                best, best_source = steps, source
            if len(best) >= 4:
                break
        return {
            "recipe_processed": "\n".join(f"{i}. {s}" for i, s in enumerate(best, 1)),
            "processed_source": best_source,
            "processed_by": self.name if best else "",
        }


class LLMProcessor(BaseProcessor):
    """
    LLM 기반 정제 (선택).

    규칙 기반이 구어체에서 한계를 보일 때 사용함. 호출 비용이 있으므로
    전체가 아니라 '규칙 결과가 빈약한 건' 만 골라 재처리하는 운영을 권장함.
    API 키가 없으면 자동으로 규칙 기반으로 폴백함.
    """

    name = "llm"

    PROMPT = (
        "다음은 유튜브 요리 영상의 원문이다. 조리 과정만 뽑아 번호 매긴 단계로 정리해라.\n"
        "규칙:\n"
        "- 인사/구독요청/잡담 제외\n"
        "- 재료 분량, 불 세기, 시간은 반드시 보존\n"
        "- 각 단계는 한 줄, '~기' 형태로 종결\n"
        "- 원문에 없는 내용 추가 금지 (추측·창작 금지)\n"
        "- 번호 목록만 출력하고 다른 말은 쓰지 말 것\n\n"
        "요리명: {dish}\n원문:\n{text}"
    )

    def __init__(self, model: str = "claude-sonnet-4-6", min_rule_steps: int = 3) -> None:
        self.model = model
        self.min_rule_steps = min_rule_steps
        self._fallback = RuleBasedProcessor()

    def process(self, record) -> dict:
        # 먼저 규칙 기반으로 처리 — 결과가 충분하면 LLM 호출 생략(비용 절감)
        rule_result = self._fallback.process(record)
        step_count = len(
            [ln for ln in rule_result["recipe_processed"].splitlines() if ln.strip()]
        )
        if step_count >= self.min_rule_steps:
            return rule_result

        if not S.ANTHROPIC_API_KEY:
            logger.debug("ANTHROPIC_API_KEY 없음 — 규칙 기반 결과 사용")
            return rule_result

        # 가장 내용이 많은 원문을 LLM 에 전달
        source, text = "", ""
        for src in S.SOURCE_PRIORITY:
            candidate = getattr(record, src, "") or ""
            if len(candidate) > len(text):
                source, text = src, candidate
        if not text:
            return rule_result

        try:
            import anthropic
            client = anthropic.Anthropic(api_key=S.ANTHROPIC_API_KEY)
            resp = client.messages.create(
                model=self.model, max_tokens=1000,
                messages=[{
                    "role": "user",
                    "content": self.PROMPT.format(
                        dish=getattr(record, "recipe_name", ""), text=text[:4000]
                    ),
                }],
            )
            out = "".join(b.text for b in resp.content if b.type == "text").strip()
            if out:
                return {
                    "recipe_processed": out,
                    "processed_source": source,
                    "processed_by": self.name,
                }
        except ImportError:
            logger.warning("anthropic 미설치 — 규칙 기반으로 폴백")
        except Exception as e:
            logger.warning("LLM 정제 실패: %s", e)
        return rule_result


PROCESSOR_REGISTRY: dict[str, type[BaseProcessor]] = {
    RuleBasedProcessor.name: RuleBasedProcessor,
    LLMProcessor.name: LLMProcessor,
}


def get_processor(name: str) -> BaseProcessor:
    """이름으로 정제기 인스턴스 생성 (없으면 규칙 기반)."""
    cls = PROCESSOR_REGISTRY.get(name, RuleBasedProcessor)
    return cls()
