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
SSH_OPTS=(
  -F /dev/null
  -o BatchMode=yes
  -o ConnectTimeout=8
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
)

log() {
  printf '[deploy] %s\n' "$*"
}

remote_run() {
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "$@"
}

log "Loesche den Remote-Ordner ${REMOTE_DIR} und stelle sicher, dass der Daten-Ordner existiert"
remote_run "set -eu
# vOpus wird komplett gewischt
rm -rf '${REMOTE_DIR}'
mkdir -p '${REMOTE_DIR}'
# Sicherstellen, dass der persistente Daten-Ordner und die m3u-Datei (als Datei!) existieren
mkdir -p '/storage/docker/partyqueue/data'
mkdir -p '/storage/docker/partyqueue/state'
touch '/storage/docker/partyqueue/data/kodi.m3u'"

log "Stelle UI-State-Volume in /storage/docker-compose.yml sicher"
remote_run "set -eu
COMPOSE_FILE='/storage/docker-compose.yml'
if [ -f \"\$COMPOSE_FILE\" ]; then
  sed -i 's#/data/playlists/telegram_ui_state.json#/data/state/telegram_ui_state.json#g' \"\$COMPOSE_FILE\"
  if ! grep -q '/storage/docker/partyqueue/state:/data/state' \"\$COMPOSE_FILE\"; then
    sed -i '\\#/storage/docker/partyqueue/playlists:/data/playlists#a\\      - /storage/docker/partyqueue/state:/data/state' \"\$COMPOSE_FILE\"
  fi
  if ! grep -q 'BOT_LANGUAGE:' \"\$COMPOSE_FILE\"; then
    sed -i '/TELEGRAM_BASE_FILE_URL:/a\\      BOT_LANGUAGE: \"\${BOT_LANGUAGE:-en}\"' \"\$COMPOSE_FILE\"
  fi
fi"

log "Kopiere kodi.m3u nach LibreELEC..."
scp "${SSH_OPTS[@]}" "${LOCAL_ROOT}/data/kodi.m3u" "${SSH_TARGET}:/storage/docker/partyqueue/data/kodi.m3u"

log "Kopiere Projektdateien per scp"
scp "${SSH_OPTS[@]}" -r \
  "${LOCAL_ROOT}/.dockerignore" \
  "${LOCAL_ROOT}/Caddyfile" \
  "${LOCAL_ROOT}/Dockerfile" \
  "${LOCAL_ROOT}/README.md" \
  "${LOCAL_ROOT}/main.py" \
  "${LOCAL_ROOT}/kodibot" \
  "${LOCAL_ROOT}/scripts" \
  "${LOCAL_ROOT}/assets" \
  "${SSH_TARGET}:${REMOTE_DIR}/"

log "Entferne Python-Bytecode und pycache aus ${REMOTE_DIR}"
remote_run "REMOTE_DIR='${REMOTE_DIR}' sh -s" <<'REMOTE_SCRIPT'
set -eu
find "$REMOTE_DIR" -type d -name 'pycache' -prune -exec rm -rf {} +
find "$REMOTE_DIR" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
REMOTE_SCRIPT

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
