# Kodi Media Bot for Telegram

This bot controls Kodi and a CEC device (HiFi/TV) via Telegram.

Besides YouTube and SoundCloud queue links, the bot can also play Telegram
voice/video uploads and selected social-media video URLs directly once.
Those temporary media files are deleted again after playback stops.

## Files
- `main.py`: entrypoint.
- `telegram_ui.py`: Telegram UI handlers.
- `queue_state.py`: queue + playback state.
- `kodi_api.py`: Kodi JSON-RPC + WS helpers.
- `playlist_store.py`: save/load playlist JSON.
- `telegram_media.py`: Telegram uploads temporaer speichern, direkt an Kodi ausliefern und wieder loeschen.
- `Dockerfile`: builds the image.

## Build
From this folder:
```
docker build -t partyqueue .
```

## Kodi addons (LibreELEC)
SoundCloud and YouTube addons are required on LibreELEC.

To get the SoundCloud client id:
```
cat /storage/.kodi/userdata/addon_data/plugin.audio.soundcloud/cache/api-client-id
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
  -e KODI_HOST=172.17.0.1 \
  -e DENON_HOST="DENON_IP" \
  -e KODI_PORT=8080 \
  -e KODI_WS_PORT=9090 \
  -e CEC_HOST=172.17.0.1 \
  -e DEBUG_WS=1 \
  -e KODI_USER="USER" \
  -e KODI_PASS="Password" \
  -e SC_CLIENT_ID="YOUR_CLIENT_ID" \
  -e MEDIA_BASE_URL="http://YOUR_HOST_IP:8765" \
  -v /storage/docker/partyqueue:/root/.ssh:ro \
  -v /storage/docker/partyqueue/playlists:/data/playlists \
  -v /storage/docker/partyqueue/uploads:/data/uploads \
  partyqueue
```

## Docker Compose
```bash
services:
  kodi-media-bot:
    build: .
    container_name: kodi-media-bot
    restart: unless-stopped
    network_mode: host
    environment:
      TG_TOKEN: "YOUR_TELEGRAM_BOT_TOKEN"
      KODI_HOST: "172.17.0.1"
      KODI_PORT: "8080"
      KODI_WS_PORT: "9090"
      KODI_USER: "USER"
      KODI_PASS: "Password"
      CEC_HOST: "172.17.0.1"
      DENON_HOST: "DENON_IP"
      DEBUG_WS: "1"
      SC_CLIENT_ID: "YOUR_CLIENT_ID"
      MEDIA_BASE_URL: "http://YOUR_HOST_IP:8765"
    volumes:
      - /storage/docker/partyqueue:/root/.ssh:ro
      - /storage/docker/partyqueue/playlists:/data/playlists
      - /storage/docker/partyqueue/uploads:/data/uploads
```

Starten:
```bash
docker compose up -d --build
```

Notes:
- `--network host` is required so the bot can reach Kodi JSON-RPC on the host.
- `KODI_HOST` is used for Kodi JSON-RPC.
- `DENON_HOST` is used for AirPlay status detection (`/goform/formNetAudio_StatusXml.xml`), main-zone volume readout (`/goform/formMainZone_MainZoneXml.xml`), Denon power control (`/goform/formiPhoneAppPower.xml`), and volume up/down via HTTP direct commands (`/goform/formiPhoneAppDirect.xml?MVUP` / `MVDOWN`) on the Denon receiver.
- Denon requirement: on the receiver, set `Setup -> Netzwerk -> Netzwerk-Steuerung -> Immer ein`, otherwise LAN control/status may fail in standby.
- `CEC_HOST` is used for CEC over SSH. If not set, it falls back to `KODI_HOST`.
- If `DENON_HOST` is set, volume buttons use Denon HTTP direct control; otherwise volume buttons use CEC over SSH via `CEC_HOST`.
- `KODI_USER`/`KODI_PASS` configure Kodi JSON-RPC auth and are required.
- `KODI_WS_PORT` configures the Kodi websocket port.
- `DEBUG_WS=1` enables websocket debug logging.
- `SC_CLIENT_ID` configures the SoundCloud client id.
- `UPLOAD_DIR` sets the local upload directory (default `/data/uploads`).
- `MEDIA_SERVER_HOST` configures the bind address of the built-in upload server (default `0.0.0.0`).
- `MEDIA_SERVER_PORT` configures the port of the built-in upload server (default `8765`).
- `MEDIA_BASE_URL` should point to the bot host from Kodi's point of view, for example `http://192.168.1.20:8765`.
- `MEDIA_SERVER_PUBLIC_HOST` is an optional fallback when `MEDIA_BASE_URL` is not set.
- The image includes `ffmpeg` so Telegram MP4 uploads can be remuxed with `+faststart` before playback.
- `kodi.m3u` is copied into the image as `/data/kodi.m3u` and is used to map channel names to stream URLs for ICY now-playing title lookup.
- `RADIO_M3U_PATH` optionally overrides the M3U path (default: `/data/kodi.m3u`).
- `RADIO_STREAM_MAP` is optional and overrides entries from `kodi.m3u`. Example: `{"Radioactive Sifnos":"https://streamyourdream.org:8050/radioactive"}`.
- `ICY_TITLE_TTL` (seconds, default `15`) configures how long ICY titles are cached.
- `ICY_TIMEOUT` (seconds, default `6`) configures the ICY metadata fetch timeout.
- `RADIO_YT_TTL` (seconds, default `21600`) configures how long resolved YouTube links for radio tracks are cached.
- `RADIO_YT_FAIL_TTL` (seconds, default `300`) configures how long failed YouTube lookups are cached.
- `RADIO_YT_TIMEOUT` (seconds, default `8`) configures the timeout for `yt-dlp` YouTube search.
- Playlists are saved to `/data/playlists` inside the container. Mount a host path to persist them.
- Telegram uploads are stored temporarily in `/data/uploads` inside the container, deleted again after playback stops, and old leftovers are cleaned up on bot startup.
- Social-media video links from supported domains like TikTok, Instagram, Facebook and X/Twitter are downloaded once with `yt-dlp`, played directly, and then deleted again.
- Use the “Save” and “Load” buttons in the Telegram panel to store or restore the queue.

### `KODI_HOST` vs `MEDIA_BASE_URL`
- `KODI_HOST=172.17.0.1` is used by the bot to reach Kodi JSON-RPC.
- `MEDIA_BASE_URL` is used by Kodi to reach the bot's temporary upload server.
- These values can be the same host, but they have different directions.
- If Kodi can reach the bot under `http://172.17.0.1:8765`, then `MEDIA_BASE_URL=http://172.17.0.1:8765` is fine.
- If Kodi cannot reach that address, use the real LAN IP of the Docker host instead, for example `http://192.168.178.10:8765`.

## Troubleshooting
- `ssh: not found`: install `openssh-client` in the image.
- `Host key verification failed`: the bot uses SSH options to skip host key checks.
- `Permission denied`: key not mounted or permissions too open; re-check SSH setup.
