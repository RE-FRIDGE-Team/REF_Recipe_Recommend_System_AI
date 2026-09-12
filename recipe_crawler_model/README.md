# 유튜브 자취요리 레시피 수집기

자취생 대상 유튜브 요리 영상에서 레시피를 수집해 CSV 로 저장함.
음성 인식(faster-whisper)이 베이스, 자막은 보조.

## 폴더 구조
```
REF_Classification_For_Ingredient_Recognition/
├── recipe_crawler_model/                    # 크롤러 코드
│   ├── crawler_config.py                       # 채널·키워드·모델·스키마 설정
│   ├── youtube_video_list_collector.py         # 1·2단계 영상목록 수집 + 중복제거
│   ├── audio_transcriber_subtitle_fetcher.py   # 3단계 음성인식 + 자막
│   ├── recipe_field_extractor.py               # 4단계 요리명·재료·조리시간 추출
│   ├── recipe_step_normalizer.py               # 4단계 원문 → 번호 단계 정형화
│   ├── run_recipe_crawler.py                   # 실행 진입점
│   ├── requirements.txt
│   └── data/                                   # 작업용 (git 제외)
│       ├── audio/                              # 임시 음성 (처리 후 삭제)
│       └── cache/                              # 체크포인트 JSONL
└── recipe_data_collection/                     # 최종 CSV 만 저장
    └── REF_Youtube_Recipe_20260919_1430.csv
```

## 실행
repo 루트에서 `-m` 으로 실행할 것 (상대 임포트 사용).

```bash
pip install -r recipe_crawler_model/requirements.txt
export YOUTUBE_API_KEY="..."        # Windows: set YOUTUBE_API_KEY=...

python -m recipe_crawler_model.run_recipe_crawler --collect-only        # 목록만
python -m recipe_crawler_model.run_recipe_crawler --no-asr --limit 20   # 자막만, 빠른 검증
python -m recipe_crawler_model.run_recipe_crawler                       # 전체
python -m recipe_crawler_model.run_recipe_crawler --drop-raw            # 원문 제외본
```

## 환경 (Python 3.13 + PyCharm)
`ctranslate2` 는 4.6.0 부터 3.13 휠 제공. 그 아래는 3.13 설치 불가하니 하한 유지할 것.

PyCharm Run Configuration:
1. Settings → Python Interpreter → Add → Virtualenv (Python 3.13)
2. Run/Debug Configurations → **Module name** 에 `recipe_crawler_model.run_recipe_crawler`
   (Script path 아님)
3. Working directory = repo 루트
4. Environment variables 에 `YOUTUBE_API_KEY` 등록

의존성은 루트 requirements.txt 와 분리함. 기존 ML 파이프라인은 Docker Python 3.11,
크롤러는 3.13 이고 faster-whisper/yt-dlp 는 분류 학습에 불필요해서 도커만 무거워짐.

## 도커 실행

기존 ML 파이프라인 이미지(Python 3.11 + JDK)와 분리함. 크롤러는 3.13 + ffmpeg 가 필요하고,
faster-whisper/yt-dlp 는 분류 학습에 불필요해서 기존 이미지만 무거워지기 때문임.

**준비** — repo 루트에 `.env` 생성 (git 제외 대상):
```
YOUTUBE_API_KEY=발급받은키
```

**CPU 실행** (기본):
```bash
cd recipe_crawler_model
docker compose -f docker-compose.crawler.yml build
docker compose -f docker-compose.crawler.yml run --rm crawler --no-asr --limit 5
docker compose -f docker-compose.crawler.yml run --rm crawler --limit 50
docker compose -f docker-compose.crawler.yml run --rm crawler --drop-raw
```
`run --rm` 뒤에 붙이는 인자가 그대로 CLI 로 전달됨(ENTRYPOINT 방식).

**GPU 실행** — `nvidia-container-toolkit` 필요:
```bash
docker compose -f docker-compose.crawler.yml --profile gpu build crawler-gpu
docker compose -f docker-compose.crawler.yml --profile gpu run --rm crawler-gpu --limit 50
```
확인: `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu24.04 nvidia-smi`

**볼륨 구성**

| 볼륨 | 용도 | 비고 |
|---|---|---|
| `../recipe_data_collection` (바인드) | 최종 CSV | 호스트에서 바로 열람 |
| `crawler-data` | 체크포인트 JSONL + 임시 음성 | 중단 후 이어서 실행하려면 유지 필요 |
| `whisper-cache` | whisper 모델 가중치 | 없으면 매번 수 GB 재다운로드 |

CSV 는 바인드 마운트라 컨테이너에서 `docker cp` 없이 호스트 `recipe_data_collection/` 에 바로 생김.

**빌드 컨텍스트가 repo 루트인 이유**: `COPY recipe_crawler_model/` 경로를 쓰기 때문.
루트에 `.dockerignore` 를 반드시 추가할 것(없으면 `results/`, `models/`, `.git` 까지 빌드
컨텍스트로 전송돼 빌드가 느려짐). 내용은 `dockerignore_for_repo_root.txt` 참고.

**GPU 이미지 주의**: CUDA + cuDNN 베이스라 수 GB 로 무거움. Ubuntu 24.04 기본 파이썬이
3.12 라 deadsnakes PPA 로 3.13 을 설치함. 3.12 로 충분하면 그 단계를 빼고 단순화 가능함.

## CSV 스키마 (28컬럼, 컬럼명 영어 / 값 한국어)

| 그룹 | 컬럼 |
|---|---|
| 식별자 | `video_id` `video_url` `video_title` `channel_name` `channel_id` |
| 추출 | `dish_name` `ingredients` `ingredients_json` `cook_time_display` `cook_time_min` `servings` |
| 정형화 | `processed_recipe` `processed_step_count` `processed_source` |
| 원문 | `recipe_audio` `recipe_subtitle` `description` `pinned_comment` |
| 품질 | `subtitle_is_manual` `subtitle_lang` `asr_model` `asr_avg_logprob` `extraction_source` `is_pinned_verified` |
| 메타 | `duration_sec` `published_at` `view_count` `collected_at` |

주요 컬럼:
- `video_id` — 중복제거 키. 제목은 중복되지만 ID 는 불변
- `subtitle_is_manual` — 자막 신뢰도. 수동(True)/자동(False)/없음(None)
- `processed_recipe` — 사족 제거한 번호 단계 레시피
- `processed_source` / `extraction_source` — 어느 원문에서 뽑았는지 (품질 확인용)
- `asr_avg_logprob` — ASR 품질 신호. 낮으면(≲-1.0) 인식 부정확 가능성

## 정형화 (`processed_recipe`)
원문의 인사·구독요청·잡담을 걷어내고 조리 문장만 번호 단계로 재구성함.
불 세기·시간·분량은 보존.

```
1. 다진 마늘 3큰술이랑 미림 2큰술 간장 4큰술 넣고 파 1대 다져서 섞어주기
2. 후라이팬에 식용유 3큰술 두르고 중강불로 유지하기
3. 기름이 달궈지면 목살 올려서 한 면당 1분씩 총 6분 구워주기
4. 약불로 줄이고 양념 넣어서 5분동안 조려주기
```

번호가 이미 있는 원문(설명란·고정댓글)은 구조를 살리고, 줄글(음성)은 문장을 분리해
조리 동사·계량 신호가 있는 것만 남김.

**한계**: 구어체 ASR 을 규칙만으로 완벽히 정형화하는 건 불가능함. 설명란 기반이 가장
깨끗하고 음성 기반은 빗나갈 수 있음. `processed_source` 로 확인 가능.
품질이 더 필요하면 `recipe_step_normalizer.llm_refine()` 로 LLM 후처리 연결
(기본 비활성, 호출 비용 있음).

## 추출 우선순위
고정댓글(검증) → 설명란 → 수동자막 → 음성 → 자동자막

**고정댓글은 API 로 확정 불가.** YouTube Data API v3 에 `isPinned` 필드가 없어
`order=relevance` 첫 댓글 + 작성자가 채널 주인인지 대조하는 휴리스틱을 씀.
결과는 `is_pinned_verified` 로 표시. 실제로 레시피 전문은 설명란에 더 많음.

## API 쿼터
기본 10,000 units/일. `search.list` 는 호출당 100 units (하루 100회 상한),
채널 업로드 목록은 `playlistItems` 로 1 unit. 그래서 채널 수집을 먼저 돌리고
검색은 보충용으로 씀. `--no-pinned` 로 댓글 조회를 끄면 쿼터를 더 아낌.

## 체크포인트
`data/cache/results.jsonl` 에 건건이 저장돼 중단 후 재실행 시 처리분을 스킵함.
ASR 이 영상당 수십 초라 이게 중요함.

## 주의
음성 다운로드는 유튜브 약관상 제한될 수 있음. 내부 연구·분석 목적으로만 쓰고
원본 음성이나 트랜스크립트 전문은 재배포하지 말 것. 재료·조리시간 같은 사실 정보는
저작권 대상이 아니지만 영상 내레이션 전문은 표현물이라 다름.
공개 배포 시 `--drop-raw` 로 원문 컬럼을 뺀 CSV 를 쓸 것.
