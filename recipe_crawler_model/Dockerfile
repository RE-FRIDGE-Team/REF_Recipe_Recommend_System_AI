# Dockerfile — 레시피 크롤러 CPU 이미지
#
# ffmpeg 필수: yt-dlp 의 음성 추출과 faster-whisper 의 디코딩에 쓰임.
# 없으면 ASR 이 조용히 실패하므로 반드시 포함할 것.
#
# 빌드는 repo 루트를 컨텍스트로 실행함 (COPY 경로가 recipe_crawler_model/ 기준):
#   docker build -f recipe_crawler_model/Dockerfile -t ref-recipe-crawler:cpu .

FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/cache/huggingface

WORKDIR /workspace

# 의존성 먼저 설치 — 코드가 바뀌어도 이 레이어는 캐시 재사용됨
COPY recipe_crawler_model/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /tmp/requirements.txt \
    && pip install torch --index-url https://download.pytorch.org/whl/cpu

COPY recipe_crawler_model/ /workspace/recipe_crawler_model/

RUN mkdir -p /workspace/recipe_data_collection /cache/huggingface

# 상대 임포트를 쓰므로 -m 으로 실행
ENTRYPOINT ["python", "-m", "recipe_crawler_model"]
CMD ["--help"]
