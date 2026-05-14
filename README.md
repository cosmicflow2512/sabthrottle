# sabthrottle

Drosselt **SABnzbd** automatisch, sobald **Jellyfin** aktiv streamt (z. B. via
[NZBDav](https://github.com/nzbdav-dev/nzbdav)), und gibt nach Ende des Streams
wieder die volle Bandbreite frei.

> Use case: NZBDav streamt Inhalte direkt aus dem Usenet in Jellyfin. Parallel
> läuft SABnzbd für reguläre Downloads. Ohne Drosselung kann SABnzbd den
> Stream ausbremsen — sabthrottle löst das, ohne dass du manuell eingreifst.

## Funktionsweise

Zwei Betriebsmodi — du kannst frei wählen:

### Polling-Modus (empfohlen, einfacher)

```
sabthrottle ──poll /Sessions──▶ Jellyfin API
            ──set speedlimit──▶ SABnzbd API
```

Du gibst nur `JELLYFIN_URL` + `JELLYFIN_API_KEY` an. sabthrottle fragt Jellyfin
alle 15 s (konfigurierbar) ab. **Kein Webhook-Plugin in Jellyfin nötig.** Da
jede Abfrage die volle Wahrheit liefert, gehen keine Stop-Events verloren.

### Webhook-Modus

```
Jellyfin Webhook Plugin ──Playback{Start,Stop,Progress}──▶ sabthrottle ──▶ SAB
```

Reagiert sofort (keine Polling-Latenz), braucht aber das Webhook-Plugin.
`PlaybackProgress`-Events dienen als Heartbeat; tote Sessions werden nach
`SESSION_TIMEOUT_SEC` automatisch entfernt (Schutz vor Client-Crashes).

**In beiden Modi:**
- Stream aktiv → SAB auf `THROTTLE_PERCENT` (Default 50 %)
- Kein Stream aktiv → SAB zurück auf `FULL_PERCENT` (Default 100 %)

## Konfiguration

Alles per Environment-Variablen — siehe [`config.example.env`](config.example.env).

| Variable | Pflicht | Default | Beschreibung |
|---|---|---|---|
| `SABNZBD_URL` | ✓ | — | Base-URL deiner SABnzbd-Instanz |
| `SABNZBD_API_KEY` | ✓ | — | SABnzbd API-Key |
| `JELLYFIN_URL` |  | — | Jellyfin Base-URL — aktiviert Polling-Modus |
| `JELLYFIN_API_KEY` |  | — | Jellyfin API-Key (Dashboard → API Keys) |
| `JELLYFIN_POLL_INTERVAL_SEC` |  | `15` | Poll-Intervall in Sekunden |
| `THROTTLE_PERCENT` |  | `50` | SAB-Limit (%) während aktivem Stream |
| `FULL_PERCENT` |  | `100` | SAB-Limit (%) ohne aktiven Stream |
| `WEBHOOK_TOKEN` |  | — | Optionaler Shared-Secret (nur Webhook-Modus) |
| `NZBDAV_PATH_PREFIX` |  | — | Nur drosseln, wenn der Item-Pfad diesen String enthält |
| `SESSION_TIMEOUT_SEC` |  | `90` | Heartbeat-Timeout (nur Webhook-Modus) |
| `LISTEN_PORT` |  | `6811` | HTTP-Port des Receivers |
| `LOG_LEVEL` |  | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Installation

### Unraid (empfohlen)

1. Unter *Docker → Add Container → Template URL* einfügen:
   ```
   https://raw.githubusercontent.com/cosmicflow2512/sabthrottle/main/cosmicflow2512/sabthrottle.xml
   ```
2. `SABNZBD_URL` und `SABNZBD_API_KEY` ausfüllen, Port 6811 freigeben.
3. Container starten.

### Docker Compose

```yaml
services:
  sabthrottle:
    image: ghcr.io/cosmicflow2512/sabthrottle:latest
    restart: unless-stopped
    ports:
      - "6811:6811"
    environment:
      SABNZBD_URL: "http://sabnzbd:8080"
      SABNZBD_API_KEY: "your_key"
```

### Selbst bauen

```bash
docker build -t sabthrottle .
docker run -d --name sabthrottle -p 6811:6811 \
  -e SABNZBD_URL=http://sabnzbd:8080 -e SABNZBD_API_KEY=xxx \
  sabthrottle
```

## Jellyfin einrichten

### Variante A: Polling (empfohlen)

1. In Jellyfin: **Dashboard → API Keys → +** → neuen Key erzeugen.
2. Diesen Key + die Jellyfin-URL als `JELLYFIN_API_KEY` / `JELLYFIN_URL` in
   sabthrottle setzen.
3. Fertig — kein Plugin nötig.

### Variante B: Webhook (sofortige Reaktion)

1. **Dashboard → Plugins → Catalog → Webhook** installieren, Jellyfin neu starten.
2. **Dashboard → Webhook → Add Generic Destination**:
   - **Webhook URL**: `http://<sabthrottle-host>:6811/jellyfin`
     (mit Token: `…/jellyfin?token=DEIN_TOKEN`)
   - **Notification Type**: `Playback Start`, `Playback Stop`, `Playback Progress`
   - **Item Type**: `Movies`, `Episodes` (optional)
   - **Send All Properties**: aktivieren
3. Speichern, **Test** klicken → sollte in den Logs auftauchen.

## Endpoints

| Pfad | Methode | Zweck |
|---|---|---|
| `/jellyfin` | POST | Webhook-Empfänger |
| `/status` | GET | Aktive Sessions + aktuelles SABnzbd-Limit (JSON) |
| `/health` | GET | Healthcheck |

## Troubleshooting

- **SABnzbd ändert sich nicht**: API-Key prüfen, `SABNZBD_URL` ohne trailing `/`,
  Container muss SABnzbd erreichen können (gleiches Docker-Netz oder IP).
- **Stream wird nicht erkannt**: `LOG_LEVEL=DEBUG` setzen und `/status` während
  des Streams aufrufen. Wenn Sessions nie auftauchen: Jellyfin-Webhook-Test
  schickt nichts an `/jellyfin`? URL/Port falsch.
- **SABnzbd bleibt nach Stream-Abbruch gedrosselt**: das übernimmt die GC nach
  `SESSION_TIMEOUT_SEC`. Wert ggf. kleiner setzen.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
