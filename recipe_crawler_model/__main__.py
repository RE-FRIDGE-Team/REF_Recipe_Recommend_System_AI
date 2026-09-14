"""
__main__.py — CLI 진입점.

상대 임포트를 쓰므로 repo 루트에서 -m 으로 실행할 것:
    python -m recipe_crawler_model --no-asr --limit 5

확장 메모:
    새 실행 옵션을 추가할 때 settings.RunConfig 에 필드를 넣고
    여기 add_argument 에 연결하면 파이프라인 전역에서 참조 가능함.
"""

from __future__ import annotations

import argparse
import logging

from . import settings as S
from .ingredient_normalizer import load_pgin_vocab
from .pipeline import run
from .processors import PROCESSOR_REGISTRY


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="recipe_crawler_model",
        description="유튜브 자취요리 레시피 수집기",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # 실행 범위
    p.add_argument("--limit", type=int, default=None,
                   help="처리할 영상 수 제한 (테스트용)")
    p.add_argument("--collect-only", action="store_true",
                   help="영상 목록만 수집하고 종료 (쿼터 확인용)")
    p.add_argument("--refresh-list", action="store_true",
                   help="영상 목록 캐시를 무시하고 재수집")

    # 수집 토글
    p.add_argument("--no-asr", action="store_true",
                   help="음성 인식 생략 (자막·설명란만, 훨씬 빠름)")
    p.add_argument("--no-comment", action="store_true",
                   help="고정댓글 수집 생략 (API 쿼터 절약)")
    p.add_argument("--keep-audio", action="store_true",
                   help="변환 후 음성 파일을 삭제하지 않음")

    # 정제
    p.add_argument("--processor", choices=sorted(PROCESSOR_REGISTRY), default="rule",
                   help="레시피 정제 방식 (llm 은 ANTHROPIC_API_KEY 필요)")

    # 출력
    p.add_argument("--drop-raw", action="store_true",
                   help="원문 텍스트 컬럼을 제외하고 저장 (공유용)")
    p.add_argument("--pgin-vocab", default=None, metavar="PATH",
                   help="재고 대조용 PGIN 어휘 파일 (인식 데이터셋 CSV 권장). "
                        "재료명을 재고와 같은 키로 스냅시킴")

    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p


def main() -> None:
    args = build_parser().parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # 재고 데이터와 대조하는 키는 PGIN 이므로 해당 어휘를 주입함
    if args.pgin_vocab:
        load_pgin_vocab(args.pgin_vocab)

    config = S.RunConfig(
        limit=args.limit,
        use_asr=not args.no_asr,
        fetch_comment=not args.no_comment,
        keep_audio=args.keep_audio,
        drop_raw=args.drop_raw,
        processor=args.processor,
        refresh_list=args.refresh_list,
        collect_only=args.collect_only,
    )
    run(config)


if __name__ == "__main__":
    main()
