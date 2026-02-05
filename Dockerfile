FROM python:3.12-alpine

RUN apk add --no-cache nano openssh-client

RUN apk add --no-cache nano

RUN python -m pip install --upgrade pip

RUN pip install --no-cache-dir \
    python-telegram-bot \
    requests \
    pytube \
    yt-dlp \
    websockets

COPY main.py /main.py
COPY telegram_ui.py /telegram_ui.py
COPY queue_state.py /queue_state.py
COPY kodi_api.py /kodi_api.py
COPY playlist_store.py /playlist_store.py

CMD ["python", "/main.py"]
