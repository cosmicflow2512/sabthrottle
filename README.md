# sabthrottle

Drosselt **SABnzbd** automatisch, sobald **Jellyfin** aktiv streamt (z. B. via
[NZBDav](https://github.com/nzbdav-dev/nzbdav)), und gibt nach Ende des Streams
wieder die volle Bandbreite frei.

> Use case: NZBDav streamt Inhalte direkt aus dem Usenet in Jellyfin. Parallel
> läuft SABnzbd für reguläre Downloads. Ohne Drosselung kann SABnzbd den
> Stream ausbremsen — sabthrottle löst das, ohne dass du manuell eingreifst.

## Funktionsweise

```
Jellyfin Webhook Plugin ──Playback{Start,Stop,Progress}──▶ sabthrottle ──▶ SABnzbd API
```

- Startet ein Stream → SAB wird auf `THROTTLE_PERCENT` (Default 50 %) gedrosselt
- Endet/pausiert der letzte aktive Stream → SAB geht zurück auf `FULL_PERCENT` (Default 100 %)
- `PlaybackProgress`-Events dienen als Heartbeat; tote Sessions werden nach
  `SESSION_TIMEOUT_SEC` automatisch entfernt (Schutz vor Client-Crashes)

## Konfiguration

Alles per Environment-Variablen — siehe [`config.example.env`](config.example.env).

| Variable | Pflicht | Default | Beschreibung |
|---|---|---|---|
| `SAB_URL` | ✓ | — | Base-URL deiner SABnzbd-Instanz |
| `SAB_API_KEY` | ✓ | — | SABnzbd API-Key |
| `THROTTLE_PERCENT` |  | `50` | SAB-Limit (%) während aktivem Stream |
| `FULL_PERCENT` |  | `100` | SAB-Limit (%) ohne aktiven Stream |
| `WEBHOOK_TOKEN` |  | — | Optionaler Shared-Secret zur Absicherung |
| `NZBDAV_PATH_PREFIX` |  | — | Nur drosseln, wenn der Item-Pfad diesen String enthält |
| `SESSION_TIMEOUT_SEC` |  | `90` | Heartbeat-Timeout für Sessions |
| `LISTEN_PORT` |  | `9999` | HTTP-Port des Receivers |
| `LOG_LEVEL` |  | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Installation

### Unraid (empfohlen)

1. Unter *Docker → Add Container → Template URL* einfügen:
   ```
   https://raw.githubusercontent.com/cosmicflow2512/sabthrottle/main/unraid-template.xml
   ```
2. `SAB_URL` und `SAB_API_KEY` ausfüllen, Port 9999 freigeben.
3. Container starten.

### Docker Compose

```yaml
services:
  sabthrottle:
    image: ghcr.io/cosmicflow2512/sabthrottle:latest
    restart: unless-stopped
    ports:
      - "9999:9999"
    environment:
      SAB_URL: "http://sabnzbd:8080"
      SAB_API_KEY: "your_key"
```

### Selbst bauen

```bash
docker build -t sabthrottle .
docker run -d --name sabthrottle -p 9999:9999 \
  -e SAB_URL=http://sabnzbd:8080 -e SAB_API_KEY=xxx \
  sabthrottle
```

## Jellyfin einrichten

1. **Dashboard → Plugins → Catalog → Webhook** installieren, Jellyfin neu starten.
2. **Dashboard → Webhook → Add Generic Destination**:
   - **Webhook URL**: `http://<sabthrottle-host>:9999/jellyfin`
     (mit Token: `…/jellyfin?token=DEIN_TOKEN`)
   - **Notification Type**: `Playback Start`, `Playback Stop`, `Playback Progress`
   - **Item Type**: `Movies`, `Episodes` (optional)
   - **Send All Properties**: aktivieren
3. Speichern, **Test** klicken → sollte in den Logs auftauchen.

## Endpoints

| Pfad | Methode | Zweck |
|---|---|---|
| `/jellyfin` | POST | Webhook-Empfänger |
| `/status` | GET | Aktive Sessions + aktuelles SAB-Limit (JSON) |
| `/health` | GET | Healthcheck |

## Troubleshooting

- **SAB ändert sich nicht**: API-Key prüfen, `SAB_URL` ohne trailing `/`,
  Container muss SAB erreichen können (gleiches Docker-Netz oder IP).
- **Stream wird nicht erkannt**: `LOG_LEVEL=DEBUG` setzen und `/status` während
  des Streams aufrufen. Wenn Sessions nie auftauchen: Jellyfin-Webhook-Test
  schickt nichts an `/jellyfin`? URL/Port falsch.
- **SAB bleibt nach Stream-Abbruch gedrosselt**: das übernimmt die GC nach
  `SESSION_TIMEOUT_SEC`. Wert ggf. kleiner setzen.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
