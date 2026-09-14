# REF Recipe Recommend System AI

RE:FRIDGE 레시피 추천 AI 시스템. 여러 하위 프로젝트로 구성됨.

## 프로젝트 구성

- [`recipe_crawler_model/`](./recipe_crawler_model/README.md) — 유튜브 자취요리 레시피 수집기
  (채널·검색 수집 → 음성인식·자막 → 요리명/재료/조리시간 추출 → 정제 → CSV)

## 데이터

- `recipe_data_collection/` — 각 프로젝트가 만드는 CSV 산출물 (공용)

## 개발 환경

프로젝트마다 Python 버전·의존성이 다를 수 있어 `requirements.txt`와 `Dockerfile`을
각 하위 폴더에 둠. 루트에는 공용 설정(`.gitignore`, `.dockerignore`, `.env`)만 둠.

```bash
cp .env.example .env        # 키 채우기
pip install -r recipe_crawler_model/requirements.txt
python -m recipe_crawler_model --no-asr --limit 5
```
