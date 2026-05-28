#!/usr/bin/env bash
set -euo pipefail

REMOTE_USER="root"
REMOTE_HOST="192.168.178.10"
REMOTE_DIR="/storage/docker/partyqueue/vOpus"
REMOTE_HOME="/storage"
REMOTE_CONTAINER_NAME="partyqueue"
REMOTE_IMAGE_NAME="partyqueue:latest"
REMOTE_COMPOSE_CMD="bin/docker-compose"

LOCAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_TARGET="${REMOTE_USER}@${REMOTE_HOST}"

log() {
  printf '[deploy] %s\n' "$*"
}

remote_run() {
  ssh "$SSH_TARGET" "$@"
}

log "Loesche den Remote-Ordner ${REMOTE_DIR} und stelle sicher, dass der Daten-Ordner existiert"
remote_run "set -eu
# vOpus wird komplett gewischt
rm -rf '${REMOTE_DIR}'
mkdir -p '${REMOTE_DIR}'
# Sicherstellen, dass der persistente Daten-Ordner und die m3u-Datei (als Datei!) existieren
mkdir -p '/storage/docker/partyqueue/data'
touch '/storage/docker/partyqueue/data/kodi.m3u'"

log "Kopiere kodi.m3u nach LibreELEC..."
scp "${LOCAL_ROOT}/data/kodi.m3u" "${SSH_TARGET}:/storage/docker/partyqueue/data/kodi.m3u"

log "Kopiere Projektdateien per scp"
scp -r \
  "${LOCAL_ROOT}/.dockerignore" \
  "${LOCAL_ROOT}/Caddyfile" \
  "${LOCAL_ROOT}/Dockerfile" \
  "${LOCAL_ROOT}/README.md" \
  "${LOCAL_ROOT}/go.mod" \
  "${LOCAL_ROOT}/go.sum" \
  "${LOCAL_ROOT}/cmd" \
  "${LOCAL_ROOT}/internal" \
  "${SSH_TARGET}:${REMOTE_DIR}/"


log "Entferne vorhandenen Container/Image und starte ${REMOTE_COMPOSE_CMD} up -d --build in ${REMOTE_HOME}"
remote_run "REMOTE_DIR='${REMOTE_DIR}' REMOTE_HOME='${REMOTE_HOME}' REMOTE_CONTAINER_NAME='${REMOTE_CONTAINER_NAME}' REMOTE_IMAGE_NAME='${REMOTE_IMAGE_NAME}' REMOTE_COMPOSE_CMD='${REMOTE_COMPOSE_CMD}' sh -s" <<'REMOTE_SCRIPT'
set -eu

if [ ! -f "$REMOTE_HOME/.env" ]; then
  echo "Keine .env in $REMOTE_HOME gefunden." >&2
  exit 1
fi

if [ ! -x "$REMOTE_HOME/$REMOTE_COMPOSE_CMD" ]; then
  echo "$REMOTE_HOME/$REMOTE_COMPOSE_CMD ist nicht verfuegbar." >&2
  exit 1
fi

if docker container inspect "$REMOTE_CONTAINER_NAME" >/dev/null 2>&1; then
  docker rm -f "$REMOTE_CONTAINER_NAME"
fi

if docker image inspect "$REMOTE_IMAGE_NAME" >/dev/null 2>&1; then
  docker rmi -f "$REMOTE_IMAGE_NAME"
fi

cd "$REMOTE_HOME"
"$REMOTE_COMPOSE_CMD" up -d --build
REMOTE_SCRIPT

log "Deploy abgeschlossen"
