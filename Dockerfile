# ── Build stage ──────────────────────────────────────────────────────
FROM golang:1.23-alpine AS builder

RUN apk add --no-cache git

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /kodibot ./cmd/kodibot

# ── Runtime stage ────────────────────────────────────────────────────
FROM alpine:3.20

RUN apk add --no-cache ffmpeg nano openssh-client python3 py3-pip
RUN pip3 install --no-cache-dir --break-system-packages yt-dlp

COPY --from=builder /kodibot /kodibot
COPY data/kodi.m3u /data/kodi.m3u

HEALTHCHECK --interval=30s --timeout=5s \
    CMD wget -q -O /dev/null http://localhost:8765/health || exit 1

CMD ["/kodibot"]
