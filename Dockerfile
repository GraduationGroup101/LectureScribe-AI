FROM node:22-bookworm-slim AS node-runtime

FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WHISPER_BACKEND=openrouter \
    WHISPER_LOCAL_FALLBACK=false \
    OLLAMA_LOCAL_FALLBACK=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node

WORKDIR /app
COPY requirements-render.txt .
RUN pip install --no-cache-dir -r requirements-render.txt

COPY api.py MainCode_FasterWhisper.py clean_with_Llama.py openrouter_transcription.py url_to_mp3.py ./
COPY front/ ./front/

RUN useradd --system --create-home --home-dir /home/lecturescribe lecturescribe \
    && chown lecturescribe:lecturescribe /app
USER lecturescribe

CMD ["sh", "-c", "exec uvicorn api:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1"]
