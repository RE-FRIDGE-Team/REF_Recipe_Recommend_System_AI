# 유튜브 자취요리 레시피 수집기

RE:FRIDGE 레시피 추천 시스템의 데이터 수집 파트.
유튜브 요리 영상에서 레시피를 수집·정제해 CSV로 저장한다.

---

## 1. 폴더 구조

```
REF_Recipe_Recommend_System_AI/
├── .env                    # 실제 API 키 (git 제외)
├── .env.example            # 키 템플릿 (커밋)
├── .gitignore
├── .dockerignore
├── recipe_crawler_model/           # 크롤러 코드
│   ├── __init__.py
│   ├── __main__.py                 # CLI 진입점
│   ├── settings.py                 # 채널·키워드·모델·임계값
│   ├── schema.py                   # CSV 컬럼 정의 단일 소스
│   ├── collectors.py               # 영상 목록 수집 (채널/검색)
│   ├── fetchers.py                 # 원문 수집 (음성 ASR / 자막)
│   ├── extractors.py               # 필드 추출 (요리명·재료·시간)
│   ├── ingredient_normalizer.py    # 재료 정규화 + 주재료/양념 분류
│   ├── processors.py               # 레시피 정제 (규칙 / LLM)
│   ├── pipeline.py                 # 오케스트레이션
│   ├── requirements.txt
│   ├── Dockerfile / Dockerfile.gpu
│   └── docker-compose.yml
└── recipe_data_collection/         # 최종 CSV만 저장
    └── REF_Youtube_Recipe_20260919_1430.csv
```

---

## 2. 로컬 실행 (PyCharm, Python 3.13)

### 2-1. 가상환경 + 의존성

```bash
# PyCharm: Settings → Project → Python Interpreter
#          → Add Interpreter → Virtualenv Environment → New (Python 3.13)

pip install -r recipe_crawler_model/requirements.txt

# torch는 환경에 맞게 별도 설치
pip install torch --index-url https://download.pytorch.org/whl/cpu    # CPU
pip install torch --index-url https://download.pytorch.org/whl/cu124  # GPU
```

`ctranslate2`는 4.6.0부터 3.13 휠을 제공한다. 그 아래 버전은 3.13에서 설치가 안 되니 requirements의 하한을 지킬 것.

### 2-2. API 키

```bash
cp .env.example .env
```
`.env`를 열어 `YOUTUBE_API_KEY=` 뒤에 키를 넣는다.
발급: Google Cloud Console → API 및 서비스 → 사용자 인증 정보 → API 키
(YouTube Data API v3를 **활성화**해야 한다)

### 2-3. 실행

repo 루트에서 `-m`으로 실행한다(상대 임포트 사용).

```bash
# ① 목록만 수집해서 쿼터·채널 설정 확인 (ASR 없음, 빠름)
python -m recipe_crawler_model --collect-only

# ② 자막·설명란만으로 소량 테스트 (ASR 생략 → 수 초)
python -m recipe_crawler_model --no-asr --limit 5

# ③ 전체 실행 (음성 인식 포함, 영상당 수십 초)
python -m recipe_crawler_model --limit 50

# ④ 재료명을 재고와 같은 키(PGIN)로 정규화 (권장)
#    인식 데이터셋 CSV 를 그대로 주면 PGIN 어휘 + GIN→PGIN 매핑이 자동 구성됨
python -m recipe_crawler_model --pgin-vocab ../recognition_dataset_augmented.csv

# ⑤ 원문 제외하고 저장 (공유용)
python -m recipe_crawler_model --drop-raw
```

**PyCharm Run Configuration**
1. Run → Edit Configurations → `+` → Python
2. **Module name**에 `recipe_crawler_model` (Script path 아님)
3. Working directory = repo 루트
4. Parameters에 `--no-asr --limit 5` 등

### 2-4. 전체 옵션

| 옵션 | 설명 |
|---|---|
| `--limit N` | 처리할 영상 수 제한 |
| `--collect-only` | 목록 수집만 하고 종료 |
| `--refresh-list` | 영상 목록 캐시 무시하고 재수집 |
| `--no-asr` | 음성 인식 생략 (자막·설명란만) |
| `--no-comment` | 고정댓글 수집 생략 (쿼터 절약) |
| `--keep-audio` | 변환 후 음성 파일 보존 |
| `--processor rule\|llm` | 정제 방식 (llm은 `ANTHROPIC_API_KEY` 필요) |
| `--drop-raw` | 원문 컬럼 제외하고 저장 |
| `--pgin-vocab PATH` | **재고 대조용 PGIN 어휘** (인식 데이터셋 CSV 권장) |

---

## 3. 도커 실행

```bash
cd recipe_crawler_model

# CPU
docker compose build
docker compose run --rm crawler --no-asr --limit 5
docker compose run --rm crawler --limit 50

# GPU (nvidia-container-toolkit 필요)
docker compose --profile gpu build crawler-gpu
docker compose --profile gpu run --rm crawler-gpu --limit 50
```

`run --rm` 뒤 인자가 그대로 CLI로 전달된다(ENTRYPOINT 방식).

**볼륨**

| 볼륨 | 용도 |
|---|---|
| `../recipe_data_collection` (바인드) | 최종 CSV — 호스트에서 바로 열람 |
| `crawler-data` | 체크포인트·임시 음성 — 중단 후 이어서 실행 |
| `whisper-cache` | 모델 가중치 — 없으면 매번 수 GB 재다운로드 |

GPU 확인: `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu24.04 nvidia-smi`

---

## 4. CSV 스키마 (32컬럼 / 컬럼명 영어, 값 한국어)

### 식별
`video_id` `video_title` `channel_name` `video_url`

`video_id`가 중복 판정 1차 키다. 제목·채널명은 바뀔 수 있지만 ID는 불변이라 신뢰도가 높다.

### 레시피 핵심
`recipe_name` `recipe_ingredients` `cook_time` `recipe_processed`

### 추천 시스템 연동 (구조화)
| 컬럼 | 예시 | 용도 |
|---|---|---|
| `ingredients_json` | `[{"name":"목살","amount":"300","unit":"g","role":"main"}]` | 구조화 원본 |
| `main_ingredients` | `목살\|마늘\|대파` | **재고 매칭 대상 (PGIN 키)** |
| `seasoning_ingredients` | `간장\|미림\|설탕` | 상비 가정, 매칭 제외 |
| `main_ingredient_count` | `3` | 매칭률 분모 |
| `main_ingredient_mediums` | `돼지고기\|채소\|채소` | 대체 판정용 중분류 (main과 순서 일치) |
| `cook_time_minutes` | `15` | 정렬·필터 |
| `servings` | `2인분` | |

### 원문
`recipe_desc` `recipe_comment` `recipe_audio` `recipe_subtitle`

### 품질·출처
`desc_has_recipe` `comment_has_recipe` `comment_is_uploader` `subtitle_is_manual` `subtitle_lang` `asr_model` `asr_confidence` `ingredients_source` `processed_source` `processed_by`

### 영상 메타
`duration_sec` `published_at` `view_count` `collected_at`

---

## 5. 추천 시스템과의 연결

수집 단계에서 **재료를 주재료와 양념으로 분리**한다. 추천 로직에서 이게 중요한 이유:

간장·소금·설탕 같은 양념까지 매칭 분모에 넣으면 거의 모든 레시피가 "재료 부족"으로 떨어진다. 양념은 대개 상비 품목이고 유통기한 압박도 적다. 그래서 매칭률은 **주재료 기준**으로 계산해야 추천이 실용적으로 나온다.

```python
# 추천 쪽에서 이렇게 쓰면 된다
fridge = {"목살", "마늘", "양파"}
recipe_mains = set(row["main_ingredients"].split("|"))

matched = fridge & recipe_mains
ratio = len(matched) / len(recipe_mains)

# ratio == 1.0  → 바로 만들 수 있음
# 0.5 <= ratio  → 일부 구매 필요
# ratio < 0.5   → 추천 부적합
missing = recipe_mains - fridge
```

**재료명 정규화**도 여기서 처리한다. 재고 대조 키는 **PGIN**(`processed_grocery_item_name`, 491종)이다.

GIN(`grocery_item_name`)은 `란 → 계란`처럼 부정확한 표기가 섞여 있어 매핑 소스로 쓰지 않는다. 대신 PGIN에 딸린 **카테고리(대분류/중분류)**를 함께 싣는다. 실측 결과 PGIN→카테고리는 거의 완전히 일관된다(491종 중 대분류 불일치 1건, 중분류 2건).

```
국내산 돼지고기 목살 → 목살   육류/계란 / 돼지고기
다진 마늘           → 마늘   채소/곡물류 / 채소
계란 2개            → 계란   육류/계란 / 계란
대파(흰 부분)       → 대파   채소/곡물류 / 채소
```

### 카테고리는 '대체'가 아니라 '해석'에 쓴다

레시피는 `돼지고기 목살`처럼 카테고리명을 앞에 붙여 쓰는 경우가 많은데, 재고의 PGIN은 `목살`이라 표기가 어긋난다. 카테고리를 단서로 삼아 이런 표기를 같은 PGIN으로 모아준다.

| 입력 | → PGIN | 해석 방식 |
|---|---|---|
| `돼지고기 목살` | `목살` | `category_assisted` |
| `소고기 등심` | `소 등심` | `category_assisted` |
| `닭 가슴살` | `닭가슴살` | `exact` (띄어쓰기 무시) |
| `다진 마늘` | `마늘` | `exact` |
| `돼지고기` | `돼지고기` | `category` (범주 지칭) |
| `트러플오일` | `트러플오일` | `unmatched` |

`소고기 등심`이 대표적이다. PGIN이 `소 등심`이라 단순 부분일치로는 못 잡는데, 중분류 `소고기`로 후보를 좁힌 뒤 `등심`을 맞춰 찾는다.

**해석 순서**
1. `exact` — PGIN 직접 일치 (띄어쓰기 차이 무시)
2. `category_assisted` — 이름에 섞인 카테고리명으로 후보를 좁혀 나머지와 매칭
3. `partial` — 이름에 포함된 가장 긴 PGIN
4. `category` — 이름 자체가 카테고리 (특정 품목이 아닌 범주 지칭)
5. `unmatched` — PGIN 미등재

방식은 `ingredients_json`의 `match_type`에 항목별로 기록된다. `unresolved_ingredients` 컬럼에는 미등재 재료만 모아둬서, PGIN 어휘를 어디부터 보강할지 바로 알 수 있다.

### 유사 재료 대체는 아직 미적용

`main_ingredient_mediums`로 중분류를 싣지만, 현 단계에서는 대체 판정에 쓰지 않는다. 중분류 폭이 카테고리마다 너무 달라서다 — `돼지고기`(13종)는 목살↔삼겹살처럼 대체가 성립하지만 `채소`(37종)는 마늘로 대파를 대체할 수 없다. 제대로 하려면 중분류보다 세밀한 유사도 테이블이 필요하고, 이는 추천 시스템 설계가 구체화된 뒤 붙이는 게 맞다.

유통기한 기반 우선순위는 추천 쪽에서 `main_ingredients`와 재고의 유통기한을 조인해 계산하면 된다. 크롤러는 매칭 가능한 키를 제공하는 데까지만 책임진다.

---

## 6. 확장 방법

| 하고 싶은 것 | 방법 |
|---|---|
| 채널·키워드 추가 | `settings.py`의 `TARGET_CHANNELS` / `SEARCH_QUERIES` |
| 새 수집 경로 (재생목록 등) | `collectors.BaseCollector` 상속 → `COLLECTOR_REGISTRY` 등록 |
| 새 원문 소스 (블로그 등) | `fetchers.BaseFetcher` 상속 → `FETCHER_REGISTRY` 등록 |
| 새 추출 필드 (난이도·칼로리) | `extractors.BaseExtractor` 상속 → `EXTRACTOR_REGISTRY` 추가 |
| 정제 방식 교체 | `processors.BaseProcessor` 상속 → `PROCESSOR_REGISTRY` 등록 |
| 컬럼 추가 | `schema.py`의 `CSV_COLUMNS` + `RecipeRecord` 둘 다 |
| 소스 우선순위 변경 | `settings.SOURCE_PRIORITY` 순서 조정 |
| 양념 사전 보강 | `ingredient_normalizer.SEASONINGS` |
| PGIN 어휘 갱신 | `--pgin-vocab` 에 최신 인식 데이터셋 CSV 지정 |

레지스트리에 등록만 하면 파이프라인이 자동으로 인식하므로, 기존 코드를 고칠 필요가 없다.

---

## 7. 동작 원리 (알아둘 것)

### 소스 우선순위
```
고정댓글(업로더 검증) → 설명란 → 음성(ASR) → 자막
```
업로더가 정리해 둔 텍스트가 구어체 음성보다 정확하다. `settings.SOURCE_PRIORITY`로 순서를 바꿀 수 있다.

### 고정댓글은 API로 확정할 수 없다
YouTube Data API v3에 `isPinned` 필드가 없다. `order=relevance`의 첫 댓글이 고정댓글일 가능성이 높다는 휴리스틱을 쓰고, 작성자가 채널 주인과 일치하면 `comment_is_uploader=True`로 표시한다. False인 값은 일반 댓글일 수 있다.

실제로는 **레시피 정리본이 설명란에 있는 경우가 더 많다.** `desc_has_recipe` / `comment_has_recipe` 플래그로 계량 표현·재료 헤더를 검사해 정리본 여부를 판별한다.

### 자막 신뢰도
`youtube-transcript-api`의 `is_generated`로 수동/자동을 구분한다. 수동 자막은 업로더가 직접 쓴 것이라 신뢰도가 높고, 자동 자막은 발음·잡음에 따라 품질이 들쭉날쭉해서 최하위 순위다.

### API 쿼터
기본 10,000 units/일. `search.list`는 호출당 **100 units**(하루 100회 상한), `playlistItems`는 **1 unit**이다. 그래서 채널 업로드 목록을 먼저 훑고 검색은 보충용으로 쓴다. `--no-comment`로 댓글 조회를 끄면 더 아낀다.

### 체크포인트
`recipe_crawler_model/data/cache/results.jsonl`에 건건이 저장된다. 중단 후 재실행하면 처리분을 건너뛴다. ASR이 영상당 수십 초라 이게 중요하다.

### 정제의 한계
구어체 ASR을 규칙만으로 완벽히 정형화하는 건 불가능하다. 번호가 매겨진 원문(설명란·고정댓글)은 거의 그대로 살릴 수 있지만, 음성 원문은 문장 분리·동작 판별이 빗나갈 수 있다. `processed_source`로 어느 원문에서 뽑았는지 확인할 수 있다.

대량 처리는 규칙 기반으로 1차 정제하고, 결과가 빈약한 건만 `--processor llm`으로 재처리하는 혼합 운영을 권한다. `LLMProcessor`는 규칙 결과가 3단계 미만일 때만 LLM을 호출해 비용을 아낀다.

---

## 8. 저작권

영상 원문(`recipe_audio`, `recipe_subtitle`)은 내레이션 표현물이라 재배포하지 말 것. 재료 목록·조리시간·조리 단계 같은 사실 정보는 저작권 대상이 아니다.

공유·배포용 CSV는 `--drop-raw`로 원문 컬럼을 빼고 만들면 된다. `.gitignore`도 기본적으로 `recipe_data_collection/*.csv`를 제외하도록 해뒀다(팀과 공유할 거면 그 줄을 지울 것).

음성 다운로드는 유튜브 약관상 제한될 수 있다. 수집한 음성은 텍스트 변환 후 자동 삭제된다(`--keep-audio`를 주지 않는 한).
