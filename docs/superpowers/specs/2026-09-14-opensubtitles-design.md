# Untertitel aus OpenSubtitles nachladen

Datum: 2026-09-14
Status: geplant

## Ziel

Öffnet ein Gast **Sprache → Untertitel wechseln** und der laufende Film hat
keine deutsche oder keine englische Untertitelspur, holt der Bot die fehlende
Sprache von OpenSubtitles, legt die `.srt` neben die Videodatei und hängt sie
an die laufende Wiedergabe. Die Auswahlliste erscheint danach mit den neuen
Spuren — aktiviert wird nichts.

Die Ablage neben dem Film ist der eigentliche Gewinn: Kodi findet die Datei
beim nächsten Abspielen von selbst, ohne Bot und ohne erneuten Download.

## Entscheidungen

| Frage | Entscheidung |
|---|---|
| Auslöser | Fehlt `de` oder `en` unter den vorhandenen Spuren, wird genau die fehlende Sprache geholt. Sind beide da, passiert nichts. |
| Nach dem Download | Auswahlliste neu anzeigen, keine Spur aktivieren. |
| Umfang | Filme und Serienfolgen aus der Kodi-Bibliothek. Ohne IMDb-ID kein Versuch. |
| Ablageort | Neben der Videodatei, geschrieben per SSH auf den LibreELEC-Host. |
| Zugriffsweg | SSH nach dem Muster von `get_ctimes_via_ssh`, kein neues Volume-Mount. |
| Identifikation | IMDb-ID aus der Kodi-Bibliothek. Kein `moviehash`, kein Dateinamen-Raten. |

### Warum SSH und nicht ein Volume-Mount

Der Bot-Container hat in `docker-compose.local-bot-api.yml` kein Mount auf die
Medienverzeichnisse — nur `/data/uploads`. Ein neues Mount hieße: Compose-Datei
ändern, ein Pfad-Mapping wie `UPLOAD_DIR`/`KODI_UPLOAD_DIR` pflegen und neu
deployen.

Der SSH-Weg existiert dagegen schon. `kodibot/core/kodi_library.py`
(`get_ctimes_via_ssh`) fährt heute remote Python-Skripte über
`ssh root@$CEC_HOST`, die Keys kommen aus dem `/root/.ssh`-Mount. Das Schreiben
einer `.srt` ist derselbe Mechanismus mit anderem Skript.

### Warum nicht Kodis eigenes OpenSubtitles-Addon

Kodi bringt einen Untertitel-Suchdialog mit, der per JSON-RPC aber nur
*geöffnet* werden kann (`GUI.ActivateWindow`). Die Auswahl passiert am
Fernseher mit der Fernbedienung, und der Bot erfährt das Ergebnis nicht. Für
eine Telegram-Fernsteuerung unbrauchbar.

## Architektur

### Neues Modul `kodibot/core/opensubtitles.py`

Reiner API-Client, keine Telegram-Abhängigkeiten.

| Funktion | Aufgabe |
|---|---|
| `missing_languages(av_state)` | Vergleicht vorhandene Spuren gegen die Zielsprachen und gibt die fehlenden zurück. |
| `search(imdb_id, language, season=None, episode=None)` | `GET /subtitles`, nach `download_count` sortiert, liefert die beste `file_id`. |
| `download(file_id)` | `POST /download` → temporäre URL → `.srt`-Bytes, dazu `remaining` fürs Log. |
| `fetch_for(imdb_id, language, season=None, episode=None)` | Klammer über Cache, `search` und `download`. |

Das Login-Token (24 h gültig) liegt als Modul-Global mit Zeitstempel, analog zu
den Cache-Globals in `kodi_metadata.py`. Bei HTTP 401 wird genau einmal neu
eingeloggt und der Request wiederholt.

Sprachvergleich normalisiert ISO 639-1 und 639-2: `ger`, `deu` und `de` gelten
als dieselbe Sprache, ebenso `eng` und `en`. Kodi liefert in
`get_av_settings()["subtitles"]` je nach Quelle beide Formen.

### Erweiterung `kodibot/core/kodi_library.py`

`write_subtitle_via_ssh(video_path, language, content) -> str | None`

Schreibt den Inhalt neben die Videodatei und gibt den geschriebenen Pfad
zurück, bei Fehlschlag `None`. Aufbau wie `get_ctimes_via_ssh`: Zielpfad und
Inhalt gehen über stdin an ein remote ausgeführtes `python3 -c`-Skript, alle
Shell-Argumente durch `shlex.quote`.

Dateiname nach Kodi-Konvention: Videodatei ohne Endung, plus Sprachcode, plus
`.srt` — aus `Inception.mkv` wird `Inception.de.srt`. Genau dieses Schema
erkennt Kodi beim nächsten Start automatisch.

Eine bereits vorhandene Datei wird **nicht** überschrieben. Existiert
`Inception.de.srt` schon, gilt sie als Cache-Treffer und es wird kein Download
ausgelöst.

### Erweiterung `kodibot/core/kodi_api.py`

`add_subtitle_file(path) -> bool` — `Player.AddSubtitle` mit `playerid` und
`subtitle`. Die Methode kennt keinen `enable`-Parameter und Kodi schaltet die
hinzugefügte Spur sofort sichtbar; deshalb ruft der Aufrufer danach einmal
`disable_subtitles()`.

### Anbindung in `kodibot/telegram/ui_callbacks.py`

Im Zweig `action == "subtitles"` (aktuell ab Zeile 1143), vor dem Bau der
Auswahlliste. Der bestehende Code bleibt der Normalfall; das Nachladen ist ein
vorgelagerter, jederzeit überspringbarer Schritt.

## Datenfluss

1. `get_av_settings()` liefert die vorhandenen Spuren. Fehlt weder `de` noch
   `en`, endet der Sonderweg hier — kein API-Kontakt.
2. `Player.GetItem` mit `file`, `uniqueid`, `season`, `episode`, `showtitle`
   liefert Pfad und IMDb-ID. Ohne IMDb-ID (YouTube-Stream, IPTV, lose Datei)
   endet der Sonderweg.
3. Statusmeldung in den Chat, damit die Wartezeit erklärt ist.
4. Pro fehlender Sprache: Liegt die Zieldatei schon neben dem Film oder im
   Fallback-Ordner, wird sie direkt verwendet; sonst `fetch_for(...)`.
5. `write_subtitle_via_ssh` legt die Datei ab. Schlägt das fehl, greift der
   Fallback nach `/data/uploads/subs/`; der an Kodi übergebene Pfad wird dann
   über `CFG.kodi_upload_dir` gebildet.
6. `add_subtitle_file(path)` je Datei, danach einmal `disable_subtitles()`.
7. Kurz warten (0,5 s, damit Kodi die Spur registriert hat), dann
   `get_av_settings()` erneut. Statusmeldung löschen, Auswahlliste aus dem
   frischen Zustand bauen.

Schritt 7 ist zwingend: Die Stream-Indizes aus Schritt 1 sind nach dem
Hinzufügen veraltet. Die Liste aus alten Indizes zu bauen würde bei einem Klick
die falsche Spur setzen.

Bei Serienfolgen wird bevorzugt die IMDb-ID der Episode verwendet. Fehlt sie,
greift `parent_imdb_id` der Serie zusammen mit `season_number` und
`episode_number`.

## API-Details

Basis: `https://api.opensubtitles.com/api/v1`

Pflicht-Header bei jedem Request: `Api-Key`, `User-Agent` (eigener Name plus
Version), `Accept: application/json`.

| Endpunkt | Aufruf |
|---|---|
| `POST /login` | `{username, password}` → `token`, 24 h gültig. Die Antwort kann eine abweichende Basis-URL nennen (VIP); die wird für die Session übernommen. |
| `GET /subtitles` | `imdb_id` bzw. `parent_imdb_id` + `season_number` + `episode_number`, dazu `languages`, `type` (`movie` bzw. `episode`) und
`order_by=download_count`. Die `file_id` steckt in `data[].attributes.files[]`. |
| `POST /download` | `{file_id}` mit `Authorization: Bearer <token>` → `link`, `file_name`, `remaining`, `reset_time`. Der `link` ist temporär und wird sofort abgerufen. |

Die IMDb-ID wird **ohne** `tt`-Präfix übergeben: Kodi liefert `tt1375666`,
OpenSubtitles erwartet `1375666`.

Downloads sind pro Account und Tag gedeckelt. `remaining` wird nach jedem
Download geloggt, damit sich ein leeres Kontingent im Log nachvollziehen lässt.

## Konfiguration

Vier neue Variablen, jeweils in `Config`, `Config.from_env()`,
`docker-compose.local-bot-api.yml` und `.env.local-bot-api.example`, dazu der
Abschnitt *Explanations* in der README:

| Variable | Default | Bedeutung |
|---|---|---|
| `OPENSUBTITLES_API_KEY` | leer | API-Key aus dem OpenSubtitles-Profil. Leer = Feature aus. |
| `OPENSUBTITLES_USER` | leer | Benutzername |
| `OPENSUBTITLES_PASS` | leer | Passwort |
| `OPENSUBTITLES_LANGUAGES` | `de,en` | Zielsprachen, kommagetrennt |

Ohne API-Key ist das Feature still deaktiviert: kein Fehler, keine Meldung,
Verhalten exakt wie heute.

## Fehlerbehandlung

Jeder Fehlerfall endet damit, dass die gewohnte Untertitel-Liste erscheint. Das
Nachladen darf den bestehenden Flow nie blockieren.

| Fall | Verhalten |
|---|---|
| Kein API-Key | Still übersprungen |
| Login schlägt fehl | Log-Warnung, Liste wie bisher |
| Kein Treffer für die Sprache | Hinweis im Chat, Liste wie bisher |
| Kontingent erschöpft (HTTP 406) | Meldung mit `reset_time` |
| SSH-Schreiben scheitert | Fallback in den Upload-Ordner, Hinweis, dass die Spur nur für diese Wiedergabe gilt |
| Timeout | 15 s pro Request, danach Abbruch mit Log-Eintrag |

Alle Netzwerk- und SSH-Aufrufe laufen über `asyncio.to_thread`, wie die
`kodi_api`-Aufrufe im übrigen Telegram-Code. Chat-Nachrichten gehen über die
Wrapper aus `kodibot/telegram/rate.py`.

Neue i18n-Schlüssel in `kodibot/telegram/i18n.py`, englisch und deutsch:
`subtitle_search_running`, `subtitle_search_none`, `subtitle_quota_exceeded`,
`subtitle_added_temporary`.

## Tests

Neue Datei `tests/test_opensubtitles.py` mit dem üblichen Env-Preamble,
`requests` und `subprocess.run` gemockt:

- `missing_languages` behandelt `ger`/`deu`/`de` als eine Sprache, meldet bei
  vollständigen Spuren nichts und bei halb vorhandenen nur die fehlende
- Suche baut bei Filmen `imdb_id`, bei Episoden `parent_imdb_id` +
  `season_number` + `episode_number` — jeweils ohne `tt`-Präfix
- Token-Erneuerung bei HTTP 401; bei gültigem Token kein zweiter Login
- HTTP 406 führt zu sauberem Abbruch statt Exception
- Dateiname wird zu `<Videoname>.de.srt`, auch bei Pfaden mit Leerzeichen
- Vorhandene Zieldatei verhindert den Download
- SSH-Fehlschlag löst den Upload-Fallback aus

Erweiterung in `tests/test_telegram_callbacks.py`: Sind `de` und `en` vorhanden,
wird kein OpenSubtitles-Aufruf ausgelöst.

## Nicht im Umfang

- Inhalte ohne Bibliothekseintrag (Suche über Dateinamen oder `moviehash`)
- Andere Sprachen als die in `OPENSUBTITLES_LANGUAGES` konfigurierten
- Auswahl unter mehreren Treffern — es gewinnt der meistgeladene
- Vorabladen beim Start eines Films; ausgelöst wird nur beim Öffnen der
  Untertitel-Auswahl
