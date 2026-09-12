# 유튜브 레시피 크롤러 이미지
#
# 기존 ML 파이프라인 이미지(Python 3.11 + JDK)와 분리함.
# 이유: 크롤러는 Python 3.13 + ffmpeg 필요하고, faster-whisper/yt-dlp 는
#       분류 모델 학습에 불필요해서 기존 이미지만 무거워짐.
#
# ffmpeg 필수 — yt-dlp 의 음성 추출과 faster-whisper 의 디코딩에 쓰임.
#
# 빌드:
#   docker build -f recipe_crawler_model/Dockerfile -t ref-recipe-crawler:cpu .
#
# GPU 를 쓰려면 Dockerfile.gpu 를 사용할 것(CPU 이미지로는 CUDA 가속 불가).

FROM python:3.13-slim

# ── 시스템 패키지 ────────────────────────────────────────────────
# ffmpeg  : 음성 추출/디코딩 (필수)
# ca-certificates : HTTPS 요청(YouTube API, 모델 다운로드)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── 파이썬 설정 ──────────────────────────────────────────────────
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # faster-whisper 모델 캐시 위치 — 볼륨으로 빼서 재다운로드 방지
    HF_HOME=/cache/huggingface

WORKDIR /workspace

# ── 의존성 먼저 설치 (레이어 캐시 활용) ──────────────────────────
# 코드가 바뀌어도 requirements 가 그대로면 이 레이어는 재사용됨
COPY recipe_crawler_model/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /tmp/requirements.txt \
    # CPU 전용 torch — CUDA 휠은 용량이 크고 GPU 이미지에서만 필요함
    && pip install torch --index-url https://download.pytorch.org/whl/cpu

# ── 코드 복사 ────────────────────────────────────────────────────
COPY recipe_crawler_model/ /workspace/recipe_crawler_model/

# CSV 출력 디렉터리 (compose 에서 볼륨 마운트됨)
RUN mkdir -p /workspace/recipe_data_collection /cache/huggingface

# 상대 임포트를 쓰므로 -m 으로 실행해야 함
ENTRYPOINT ["python", "-m", "recipe_crawler_model.run_recipe_crawler"]
# 기본 인자 — compose 나 CLI 에서 덮어쓸 수 있음
CMD ["--help"]
