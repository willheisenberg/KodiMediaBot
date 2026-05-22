FROM python:3.12-alpine

RUN apk add --no-cache ffmpeg nano openssh-client && \
    apk add --no-cache --repository=http://dl-cdn.alpinelinux.org/alpine/edge/testing pigpio

RUN python -m pip install --upgrade pip

RUN pip install --no-cache-dir \
    python-telegram-bot \
    requests \
    yt-dlp \
    websockets \
    pigpio

COPY main.py /main.py
COPY kodibot /kodibot

HEALTHCHECK --interval=30s --timeout=5s \
    CMD wget -q -O /dev/null http://localhost:8765/health || exit 1

CMD ["python", "/main.py"]
