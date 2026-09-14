"""
recipe_crawler_model — 유튜브 자취요리 레시피 수집기.

RE:FRIDGE 레시피 추천 시스템의 데이터 수집 파트.
유튜브 요리 영상에서 레시피를 수집·정제해 CSV 로 저장함.

모듈 구성:
    settings.py              설정 (채널·키워드·모델·임계값)
    schema.py                CSV 컬럼 정의 단일 소스
    collectors.py            영상 목록 수집 (채널/검색)
    fetchers.py              원문 수집 (음성 ASR / 자막)
    extractors.py            필드 추출 (요리명·재료·조리시간)
    ingredient_normalizer.py 재료 정규화 + 주재료/양념 분류
    processors.py            레시피 정제 (규칙 / LLM)
    pipeline.py              오케스트레이션
    __main__.py              CLI 진입점
"""

__version__ = "0.1.0"
