# Kodi Media Bot for Telegram

This bot controls Kodi and a CEC device (HiFi/TV) via Telegram.

Besides YouTube and SoundCloud queue links, the bot can also play Telegram
voice/video uploads and selected social-media video URLs directly once.
Those temporary media files are deleted again after playback stops.

## Structure
- `main.py`: Entrypoint.
- `kodibot/config.py`: Central configuration.
- `kodibot/core/`: Kodi JSON-RPC, queue state, and playlist storage.
- `kodibot/telegram/`: Telegram handlers, UI panel rendering, and integrated media server.
- `tests/`: Pytest unit tests.
- `Dockerfile`: Builds the image.

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

## Docker Compose
```yaml
services:
  telegram-bot-api:
    image: aiogram/telegram-bot-api:latest
    container_name: telegram-bot-api
    restart: unless-stopped
    network_mode: host
    environment:
      TELEGRAM_API_ID: "YOUR_API_ID"
      TELEGRAM_API_HASH: "YOUR_API_HASH"
      TELEGRAM_LOCAL: "1"
      TELEGRAM_HTTP_PORT: "8081"
    volumes:
      - telegram-bot-api-data:/var/lib/telegram-bot-api

  kodi-media-bot:
    build: .
    container_name: kodi-media-bot
    restart: unless-stopped
    network_mode: host
    depends_on:
      - telegram-bot-api
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
      TELEGRAM_LOCAL_MODE: "1"
      TELEGRAM_BASE_URL: "http://127.0.0.1:8081/bot"
      TELEGRAM_BASE_FILE_URL: "http://127.0.0.1:8081/file/bot"
      HA_HOST: "HA_IP"
      HA_PORT: "8123"
      HA_TOKEN: "YOUR_HA_TOKEN"
      HA_LIGHT_ID: "light.living_room"
      HA_WEBAPP_URL: "https://bot.example.com/app/ha-color"
      HA_COLORS_FILE: "/data/colors/ha_colors.json"
    volumes:
      - /storage/docker/partyqueue:/root/.ssh:ro
      - /storage/docker/partyqueue/playlists:/data/playlists
      - /storage/docker/partyqueue/uploads:/data/uploads
      - /storage/docker/partyqueue/colors:/data/colors
      - telegram-bot-api-data:/var/lib/telegram-bot-api:ro

  caddy-webapp:
    image: caddy:2-alpine
    container_name: kodi-media-bot-caddy
    restart: unless-stopped
    network_mode: host
    depends_on:
      - kodi-media-bot
    environment:
      WEBAPP_HOSTNAME: "bot.example.com"
      ACME_EMAIL: "you@example.com"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config

volumes:
  telegram-bot-api-data:
  caddy-data:
  caddy-config:
```

*(Note: To use the local Bot API server for uploads > 20 MB, you need your Telegram `api_id` and `api_hash` from `https://my.telegram.org`.)*

**Start the stack:**
```bash
cp .env.local-bot-api.example .env
# edit .env
docker compose -f docker-compose.local-bot-api.yml up -d --build
```

### Explanations
- **`telegram-bot-api`**: Used for large Telegram uploads. It stores files under `/var/lib/telegram-bot-api`. This volume is mounted into `kodi-media-bot` as read-only.
- **`kodi-media-bot`**: The main bot container.
- **`caddy-webapp`**: Public HTTPS reverse proxy required for the Telegram Mini App (Home Assistant color picker).

Bot data is persisted on the host in `/storage/docker/partyqueue/` (playlists, uploads, HA colors, SSH keys).

Important when switching an existing bot from the Telegram cloud Bot API to a local Bot API server:
```bash
curl -s "https://api.telegram.org/bot${TG_TOKEN}/logOut"
```

Without this one-time `logOut`, Telegram may keep the bot attached to the cloud Bot API and the local server will not behave correctly.

### HTTPS for the Home Assistant Mini App
Telegram Mini Apps require a public `https://` URL with a valid certificate. The local compose setup now includes a `caddy-webapp` reverse proxy for that purpose.

Requirements:
- A public DNS name such as `bot.example.com` pointing to your Docker host.
- Incoming ports `80/tcp` and `443/tcp` forwarded to that host.
- `WEBAPP_HOSTNAME` set to that DNS name.
- `ACME_EMAIL` set to a real email address for Let's Encrypt.
- `HA_WEBAPP_URL` set to `https://YOUR_HOST/app/ha-color`.
- In `@BotFather`, configure the bot's Main Mini App to the same URL so the group-chat `Open Live Color` button can open the Mini App directly.
- `CADDYFILE_PATH` set when the compose file is started from a different host directory than the project root.

Important:
- Keep `MEDIA_BASE_URL` for Kodi uploads separate if needed. It may stay an internal HTTP URL like `http://192.168.178.10:8765`.
- `HA_WEBAPP_URL` is only for Telegram's Mini App webview and should usually be the public HTTPS hostname served by Caddy.

Example:
```env
MEDIA_BASE_URL=http://192.168.178.10:8765
WEBAPP_HOSTNAME=bot.example.com
ACME_EMAIL=you@example.com
HA_WEBAPP_URL=https://bot.example.com/app/ha-color
CADDYFILE_PATH=/storage/docker/partyqueue/vOpus/Caddyfile
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
- `TELEGRAM_LOCAL_MODE=1` enables support for a local `telegram-bot-api` server, which is required if the bot should download Telegram uploads larger than 20 MB.
- `TELEGRAM_BASE_URL` optionally overrides the Telegram API endpoint, for example `http://127.0.0.1:8081/bot`.
- `TELEGRAM_BASE_FILE_URL` optionally overrides the Telegram file endpoint, for example `http://127.0.0.1:8081/file/bot`.
- `TELEGRAM_DOWNLOAD_SIZE_LIMIT_MB` changes the pre-check limit for cloud Bot API downloads (default `20`). Leave this unchanged unless you know your Telegram endpoint supports more.
- `TELEGRAM_READ_TIMEOUT` and `TELEGRAM_GET_FILE_READ_TIMEOUT` default to `300` seconds in the local Bot API setup so large Telegram files have enough time to be prepared and returned.
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
- `HA_HOST` is the IP address of your Home Assistant instance.
- `HA_PORT` is the port of Home Assistant (default `8123`).
- `HA_TOKEN` is your Long-Lived Access Token from Home Assistant.
- `HA_LIGHT_ID` is the entity ID of the light you want to control (e.g., `light.living_room`).
- `HA_WEBAPP_URL` optionally points to the Telegram Mini App for the HA light control. It must be an `https://` URL.
- `HA_COLORS_FILE` stores saved colors as JSON. In `docker-compose.local-bot-api.yml` it is set to `/data/colors/ha_colors.json`.
- `WEBAPP_HOSTNAME` is the public DNS hostname used by the bundled Caddy reverse proxy for the Mini App.
- `ACME_EMAIL` is the email address Caddy uses for Let's Encrypt certificate management.
- `CADDYFILE_PATH` optionally overrides the host path mounted as `/etc/caddy/Caddyfile`.
- If `HA_WEBAPP_URL` is not set, the bot falls back to `MEDIA_BASE_URL/app/ha-color` when `MEDIA_BASE_URL` itself is `https://`.
- If `HA_HOST` is set, a `🏠 Home Assistant` button appears in the panel with options to toggle the light, open `Live Color`, set a hex color, adjust brightness, save the current color, load saved colors, and delete saved colors.
- In private chats the HA menu shows the embedded `Live Color` Mini App button directly.
- In group chats the HA menu shows `Open Live Color`, which uses Telegram's Main Mini App deep link and therefore depends on the BotFather Mini App configuration above.

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
- `MEDIA DOWNLOAD FAIL ... File is too big`: this is the Telegram Bot API download limit. Normal cloud Bot API downloads stop at 20 MB. For large uploads, run a local `telegram-bot-api` server and set `TELEGRAM_LOCAL_MODE=1`, `TELEGRAM_BASE_URL`, and `TELEGRAM_BASE_FILE_URL`.
