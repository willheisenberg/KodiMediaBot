FROM python:3.12-alpine

RUN apk add --no-cache ffmpeg nano openssh-client

RUN python -m pip install --upgrade pip

RUN pip install --no-cache-dir \
    python-telegram-bot \
    requests \
    yt-dlp \
    websockets

COPY main.py /main.py
COPY kodibot /kodibot
# Only the button reference is needed at runtime; the rest of assets/ is README material.
COPY assets/panel_button_reference.png /assets/panel_button_reference.png
COPY assets/panel_button_reference_de.png /assets/panel_button_reference_de.png

# Operator-supplied display power scripts; find -exec tolerates an empty dir
COPY scripts /scripts
RUN find /scripts -type f -exec chmod +x {} +

HEALTHCHECK --interval=30s --timeout=5s \
    CMD wget -q -O /dev/null http://127.0.0.1:8765/health || exit 1

CMD ["python", "/main.py"]
