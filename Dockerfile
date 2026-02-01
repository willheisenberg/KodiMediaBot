FROM python:3.12-alpine

RUN apk add --no-cache nano openssh-client

RUN apk add --no-cache nano

RUN python -m pip install --upgrade pip

RUN pip install --no-cache-dir \
    python-telegram-bot \
    requests \
    pytube

COPY kodi_media_bot.py /kodi_media_bot.py

CMD ["python", "/kodi_media_bot.py"]
