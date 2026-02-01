# PartyQueue Bot (Docker)

This bot controls Kodi and a CEC device (HiFi/TV) via Telegram.

## Files
- `kodi_media_bot.py`: the file copied into the Docker image.
- `Dockerfile`: builds the image.

## Build
From this folder:
```
docker build -t partyqueue .
```

## SSH key setup (for CEC commands)
CEC buttons use SSH to run `cec-ctl` on the host. You need a key in the container:

1) Create a key on the host:
```
ssh-keygen -t ed25519 -f /storage/docker/partyqueue/id_ed25519
```

2) Allow the key on the host:
```
cat /storage/docker/partyqueue/id_ed25519.pub >> /storage/.ssh/authorized_keys
```

3) Fix permissions (important):
```
chmod 700 /storage/docker/partyqueue
chmod 600 /storage/docker/partyqueue/id_ed25519
chmod 644 /storage/docker/partyqueue/id_ed25519.pub
```

## Run
```
docker run -d --name partyqueue --restart unless-stopped --network host \
  -e TG_TOKEN="YOUR_TELEGRAM_BOT_TOKEN" \
  -e HOST_IP=172.17.0.1 \
  -e CEC_HOST=172.17.0.1 \
  -e KODI_USER="USER" \
  -e KODI_PASS="Password" \
  -v /storage/docker/partyqueue:/root/.ssh:ro \
  partyqueue
```

Notes:
- `--network host` is required so the bot can reach Kodi JSON-RPC on the host.
- `HOST_IP` is used for Kodi JSON-RPC.
- `CEC_HOST` is used for CEC over SSH. If not set, it falls back to `HOST_IP`.
- `KODI_USER`/`KODI_PASS` configure Kodi JSON-RPC auth and are required.

## Troubleshooting
- `ssh: not found`: install `openssh-client` in the image.
- `Host key verification failed`: the bot uses SSH options to skip host key checks.
- `Permission denied`: key not mounted or permissions too open; re-check SSH setup.
