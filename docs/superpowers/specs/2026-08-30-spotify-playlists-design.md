# Spotify-Playlists als YouTube-Videos in die Warteschlange

Datum: 2026-08-30
Status: umgesetzt

## Ziel

Ein Gast schickt einen Spotify-Link in den Chat. Der Bot löst die Trackliste
auf, sucht zu jedem Titel das passende YouTube-Video und reiht die Treffer in
die bestehende Warteschlange ein. Unterstützt werden Playlists, Alben und
Einzeltracks.

Das Ergebnis ist eine Neuinterpretation der Playlist, keine Kopie: Titel ohne
brauchbaren YouTube-Treffer fallen heraus.

## Entscheidungen

| Frage | Entscheidung |
|---|---|
| Trackquelle | Öffentliche Embed-Seite (`open.spotify.com/embed/...`), ohne Account und ohne Credentials. |
| Einreih-Zeitpunkt | Erst alle Tracks auflösen, dann geschlossen einreihen — wie `queue_playlist_async`. |
| Trefferprüfung | Streng. Passt der YouTube-Titel nicht zum Spotify-Titel, wird der Track übersprungen. |
| Link-Typen | `playlist`, `album`, `track`. Keine `spotify.link`-Kurzlinks. |

### Warum nicht die offizielle Web API

Ursprünglich war der Client-Credentials-Flow der offiziellen Web API geplant.
Beim Einrichten stellte sich heraus, dass Spotify seit Februar 2026 für
Development-Mode-Apps ein aktives Premium-Abo des App-Besitzers verlangt; ein
kostenloses Konto kann gar keine App mehr anlegen. Zusätzlich liefern von
Spotify kuratierte Playlists seit Ende 2024 404 für neu registrierte Apps.

Der Embed-Weg hat keine dieser Einschränkungen und liefert auch kuratierte
Playlists. Sein Preis ist eine undokumentierte Seitenstruktur und eine Grenze
von 100 Tracks pro Link.

Geprüft und verworfen wurde außerdem **SpotAPI**: Es liefert zwar vollständige
Playlists beliebiger Länge, zieht beim Import aber undeklariert `pymongo`,
`redis` und `websockets` nach und ist im Kern auf automatisierte
Account-Erstellung und Captcha-Umgehung ausgelegt. Sein einziger relevanter
Vorteil — mehr als 100 Tracks — liegt jenseits des ohnehin gesetzten Caps.

Bewusst nicht umgesetzt (YAGNI): Kurzlink-Auflösung, progressives Einreihen,
namentliche Auflistung der Fehlschläge, Artist- und Show-Links.

## Architektur

### `kodibot/core/spotify.py` (neu)

Reine Spotify-Schicht ohne Kodi- und ohne Telegram-Bezug, analog zur Isolation
von `radio_browser.py`.

- `parse_spotify_url(text) -> tuple[str, str] | None`
  Erkennt `open.spotify.com/[intl-xx/]{playlist,album,track}/<id>` und gibt
  `(kind, id)` zurück. Angehängte Query-Parameter (`?si=…`) werden ignoriert.
- `_fetch_entity(kind, sid) -> dict`
  Lädt `open.spotify.com/embed/<kind>/<id>` mit Browser-User-Agent (ohne ihn
  liefert Spotify eine andere Seite) und liest das JSON aus dem
  `__NEXT_DATA__`-Script-Tag, Pfad `props.pageProps.state.data.entity`.
- `fetch_tracks(kind, sid) -> list[tuple[str, str]]`
  Liefert `(artist, title)`-Paare in Playlist-Reihenfolge.
  Playlist und Album lesen `trackList[].title` / `.subtitle`; ein Track-Embed
  führt keine `trackList`, dort stehen Titel und Artists direkt auf der Entity
  (`entity.title`, `entity.artists[].name`).
  Abgeschnitten wird bei `CFG.spotify_max_tracks`.
- `EMBED_TRACK_LIMIT = 100` — die Grenze der Embed-Seite. Der Dispatch nutzt
  sie, um bei genau so vielen Treffern auf eine mögliche Kappung hinzuweisen,
  denn die Seite nennt die echte Playlist-Länge nicht.
- `SpotifyUnavailable` deckt alle Fehlerfälle ab: HTTP-Fehler, Netzwerkfehler,
  fehlendes `__NEXT_DATA__`, kaputtes JSON, unerwartete Struktur. Wichtig ist,
  dass eine geänderte Seite als Fehler auffällt und nicht als leere Playlist.

Einzige Abhängigkeit ist `requests`, bereits im Dockerfile vorhanden.

### `kodibot/core/kodi_metadata.py` (Erweiterung)

`spotify_track_to_youtube_id(artist, title) -> str` ruft das bestehende
`search_youtube_link(f"{artist} {title}", expected_title=f"{artist} - {title}")`
auf und extrahiert die 11-stellige Video-ID.

Damit erbt der Spotify-Pfad den vorhandenen Treffer-Cache (`RADIO_YT_TTL`) und
die Prüflogik `youtube_result_matches_radio_track()`. Dieselbe Playlist ein
zweites Mal geschickt kostet keine einzige neue Suche.

Ob die für ICY-Strings geschriebene Prüffunktion auch mit sauberen
Spotify-Feldern richtig urteilt, sichert ein eigener Test ab.

### `kodibot/core/queue_state.py` (Erweiterung)

`queue_spotify_async(kind, sid) -> tuple[int, int]`, baugleich zu
`queue_playlist_async`: `Semaphore(5)`, `asyncio.gather` über alle Tracks,
danach werden die Treffer in Playlist-Reihenfolge per `queue_video()`
eingereiht. Rückgabe ist `(eingereiht, gesamt)`.

Anzeigetitel ist der Spotify-String `Artist - Titel`, nicht der YouTube-Titel.
Er liest sich sauberer und spart die `fetch_youtube_title`-Runde vollständig.

### `kodibot/telegram/ui_text.py` (Erweiterung)

Ein Dispatch-Block nach dem SoundCloud-Teil und **vor** der YouTube-Erkennung
(`UI.kodi_api.YT` / `PL`). Der Block folgt dem etablierten Ablauf der
Nachbarblöcke: `sent = True` → `schedule_cleanup` → `update_list_message`.

Ablauf:
1. `parse_spotify_url` — kein Treffer, Block wird übersprungen.
2. `fetch_tracks`; `SpotifyNotConfigured` → `spotify_not_configured`,
   `SpotifyUnavailable` → `spotify_unavailable`.
3. Sofort-Toast `spotify_resolving` mit der Trackzahl, damit der Absender
   während der Auflösung nicht ins Leere schaut und den Link erneut schickt.
4. `queue_spotify_async`, danach Toast `spotify_added` mit `{added}/{total}`.

## Konfiguration

Zwei Variablen, jeweils in `Config`, `Config.from_env()`,
`docker-compose.local-bot-api.yml`, `.env.local-bot-api.example` und im
README-Abschnitt *Explanations*:

| Variable | Default | Zweck |
|---|---|---|
| `SPOTIFY_MAX_TRACKS` | `100` | Obergrenze pro Link |
| `SPOTIFY_TIMEOUT` | `10` | HTTP-Timeout beim Laden der Embed-Seite |

Credentials gibt es nicht: das Feature ist immer aktiv und braucht keine
Einrichtung.

Zum Cap: 100 Tracks bedeuten bei fünf parallelen Suchen rund 30 Sekunden
Wartezeit. Das ist die bewusst akzeptierte Folge der Entscheidung, erst
vollständig aufzulösen.

## Texte

Vier i18n-Schlüssel in `de` und `en`:

- `spotify_resolving` — „Suche {count} Titel auf YouTube…"
- `spotify_added` — „✔ Spotify: {added} von {total} Titeln eingereiht."
- `spotify_added_capped` — wie oben, plus Hinweis, dass Spotify nur die ersten
  {total} liefert
- `spotify_unavailable` — „⚠ Dieser Spotify-Link ist nicht lesbar — privat oder
  gelöscht."

## README

Der Abschnitt *Spotify links* hält fest, dass weder Account noch API-Key nötig
sind, nennt die drei Grenzen (100 Tracks, nur öffentliche Playlists, kuratierte
funktionieren hier sehr wohl) und begründet in einem eigenen Unterabschnitt,
warum die offizielle Web API nicht verwendet wird.

## Tests

`tests/test_spotify.py` mit der üblichen Env-Präambel:

- URL-Parsing über alle Formen sowie Nicht-Treffer.
- Trackabruf für Playlist, Album und Einzeltrack gegen nachgebaute
  Embed-Seiten.
- Browser-User-Agent wird gesendet.
- Cap-Verhalten bei `spotify_max_tracks`.
- Einträge ohne Titel oder Artist werden übersprungen.
- `SpotifyUnavailable` bei 404, Netzwerkfehler, fehlendem `__NEXT_DATA__`,
  kaputtem JSON und nicht unterstütztem Link-Typ.
- Trefferprüfung mit Spotify-Feldern, inklusive der Regel, dass nur der primäre
  Artist geprüft wird.
- Einreihen: Reihenfolge, Anzeigetitel, übersprungene Tracks, Fehler in der
  Suche, leere Liste.

`tests/test_telegram_spotify.py` deckt den Dispatch ab, inklusive des
Kappungshinweises bei genau `EMBED_TRACK_LIMIT` Treffern und des Nachweises,
dass ein Spotify-Link nicht im Social-Video-Download landet.

Alles gegen gefakte `requests`- und `yt-dlp`-Aufrufe, kein echtes Netz.
