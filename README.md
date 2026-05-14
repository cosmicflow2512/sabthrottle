# sabthrottle

Drosselt **SABnzbd** automatisch, sobald **Jellyfin** aktiv streamt
(z. B. via [NZBDav](https://github.com/nzbdav-dev/nzbdav)), und unterstützt
**zeitbasierte Geschwindigkeitsprofile** über eine eingebaute WebUI.

> Use case: NZBDav streamt Inhalte direkt aus dem Usenet in Jellyfin. Parallel
> läuft SABnzbd für reguläre Downloads. Ohne Drosselung kann SABnzbd den
> Stream ausbremsen — sabthrottle löst das automatisch und erlaubt zusätzlich
> Zeitfenster (z. B. „werktags tagsüber max. 50 %, nachts voll").

## Features

- 🎬 **Stream-Override** — Jellyfin streamt → SABnzbd wird gedrosselt
- 🕒 **Zeitbasierte Regeln** — Wochentage + Zeitfenster, beliebig viele Regeln
- 📊 **Multi-Limit-Resolver** — bei mehreren aktiven Limits gewinnt das kleinste
- ⏸️ **Pause-Modus** — komplettes Anhalten statt nur Drosseln
- 🔢 **Drei Einheiten** — Prozent, Mbit/s, MB/s (mit korrekter Umrechnung)
- 💾 **Persistenz** — Regeln & Einstellungen überleben Container-Neustart
- 🖥️ **WebUI** — keine YAML-Frickelei, alles im Browser

## Funktionsweise

```
                     ┌──────────────────┐
   Jellyfin /Sessions │  Resolver         │
        ──────────▶  │  (alle 15 s)      │ ─── min(KB/s) ───▶  SABnzbd API
   rules.json       │  Stream-Override   │
        ──────────▶  │  Zeitregeln        │
   settings.json    │  Default-Limit     │
        ──────────▶  └──────────────────┘
```

Jeden Tick werden alle aktuell anwendbaren Limits gesammelt, in KB/s normalisiert
und das **kleinste** angewendet. Wenn eine Regel im Pause-Modus aktiv wird,
pausiert SABnzbd komplett (Pause = KB/s 0 → gewinnt automatisch).

## Installation

### Unraid (empfohlen)

Template via *Add Container → Template URL*:
```
https://raw.githubusercontent.com/cosmicflow2512/sabthrottle/main/cosmicflow2512/sabthrottle.xml
```
Pflichtfelder ausfüllen (SABNZBD_URL, SABNZBD_API_KEY, optional Jellyfin) und
Apply. Der Container persistiert nach `/mnt/user/appdata/sabthrottle`.

### Docker Compose

```yaml
services:
  sabthrottle:
    image: ghcr.io/cosmicflow2512/sabthrottle:latest
    restart: unless-stopped
    ports:
      - "6811:6811"
    volumes:
      - ./config:/config
    environment:
      SABNZBD_URL: "http://sabnzbd:8080"
      SABNZBD_API_KEY: "your_key"
      JELLYFIN_URL: "http://jellyfin:8096"
      JELLYFIN_API_KEY: "your_key"
```

## Bedienung

Nach dem Start: `http://<host>:6811/`

- **Status** — aktuelles Limit, Grund (Stream / Regel / Default), Liste aller
  Kandidaten dieses Ticks (sortiert nach KB/s)
- **Zeitregeln** — CRUD für Regeln, jede Regel hat:
  - aktiv/inaktiv, Name, Wochentage, Start/Endzeit
  - Modus: Prozent · Mbit/s · MB/s · Pause
- **Einstellungen** — Leitungsgeschwindigkeit (für saubere %-Umrechnung),
  Default-Limit, Stream-Drosselung

### Beispielregel

Werktags tagsüber auf 30 Mbit/s drosseln:
- Tage: Mo–Fr
- Start: 07:00, Ende: 17:00
- Modus: Mbit/s, Wert: 30

Nachts komplett freischalten:
- Tage: alle
- Start: 22:00, Ende: 06:00 (über Mitternacht — funktioniert automatisch)
- Modus: Prozent, Wert: 100

## Konfiguration (Environment-Variablen)

Die meisten Werte sind nur „seeds" — beim ersten Start landen sie in
`settings.json`, danach ist die WebUI die Quelle der Wahrheit.

| Variable | Pflicht | Default | Beschreibung |
|---|---|---|---|
| `SABNZBD_URL` | ✓ | — | Base-URL deiner SABnzbd-Instanz |
| `SABNZBD_API_KEY` | ✓ | — | SABnzbd API-Key |
| `JELLYFIN_URL` |  | — | Aktiviert Jellyfin-Polling |
| `JELLYFIN_API_KEY` |  | — | Jellyfin API-Key (Dashboard → API Keys) |
| `JELLYFIN_POLL_INTERVAL_SEC` |  | `15` | Poll-Intervall |
| `THROTTLE_PERCENT` |  | `50` | Seed: Drosselung bei Stream |
| `FULL_PERCENT` |  | `100` | Seed: Default-Limit |
| `LINE_SPEED_MBIT` |  | `0` | Seed: reale Leitungsgeschwindigkeit |
| `CONFIG_DIR` |  | `/config` | Wo `rules.json`/`settings.json` liegen |
| `NZBDAV_PATH_PREFIX` |  | — | Filter: nur drosseln, wenn Item-Pfad das enthält |
| `WEBHOOK_TOKEN` |  | — | Shared-Secret für Webhook-Modus |
| `LISTEN_PORT` |  | `6811` | Port der WebUI/API |
| `LOG_LEVEL` |  | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Endpoints

| Pfad | Methode | Zweck |
|---|---|---|
| `/` | GET | Status-Dashboard (HTML) |
| `/rules` | GET | Regelübersicht (HTML) |
| `/settings` | GET | Settings (HTML) |
| `/api/status` | GET | JSON-Snapshot |
| `/api/rules` | GET/POST | Regeln auflisten / hinzufügen |
| `/api/rules/<id>` | PUT/DELETE | Regel ändern/löschen |
| `/api/settings` | GET/POST | Settings auslesen/speichern |
| `/jellyfin` | POST | Optional: Webhook-Empfänger |
| `/health` | GET | Healthcheck |

## Troubleshooting

- **SABnzbd ändert sich nicht**: API-Key prüfen, `SABNZBD_URL` ohne trailing
  `/`, Container muss SABnzbd erreichen können (gleiches Docker-Netz oder IP).
- **Stream wird nicht erkannt**: `LOG_LEVEL=DEBUG`, `/api/status` während des
  Streams aufrufen. Auf Jellyfin-Seite prüfen, dass der API-Key gültig ist.
- **Prozent-Regel und Mbit-Regel führen zu unerwartetem Ergebnis**:
  `LINE_SPEED_MBIT` setzen, sonst nimmt der Resolver 1 Gbit/s als
  Referenz für die %-Umrechnung an.

## Lizenz

MIT — siehe [LICENSE](LICENSE).
