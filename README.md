# sabthrottle

Automatically throttles **SABnzbd** and **JDownloader 2** while **Jellyfin**
is actively streaming (e.g. via [NZBDav](https://github.com/nzbdav-dev/nzbdav)),
and supports **time-based speed profiles** through a built-in WebUI.

> Use case: NZBDav streams content straight from Usenet into Jellyfin while
> SABnzbd and/or JDownloader run regular downloads in parallel. Without
> throttling, those downloads can starve the stream — sabthrottle solves
> that automatically and additionally supports time windows
> (e.g. "weekday daytime max. 50 %, full speed at night").

## Features

- 🎬 **Stream budget** — while Jellyfin streams, *all* downloaders together
  share one bandwidth budget, guaranteeing a fixed reserve for streaming
- ⚖️ **Demand-based splitting** — when several downloaders are active at
  once, the budget is divided proportionally to each one's measured speed:
  a download stuck on a slow host gets a small share and the rest goes to
  the others (equal 50/50 split available as an alternative)
- 🧲 **JDownloader 2 support** — via your MyJDownloader account; verified
  against the official JDownloader settings interface
  (`DownloadSpeedLimit` / `DownloadSpeedLimitEnabled`)
- 🕒 **Time-based rules** — independent rule sets per downloader, weekdays
  + time windows (incl. across midnight), any number of rules
- 📊 **Lowest-limit resolution** — when several limits apply at once, the
  strictest one wins; a stream never weakens a more aggressive rule
- ⏸️ **Pause mode** — stop a downloader completely instead of throttling
  (JDownloader's pause keeps connections alive, so resuming is instant)
- 🛡️ **Burst protection** (optional) — parks idle downloaders at a minimum
  speed while streaming so a download that starts mid-tick can't briefly
  saturate the line
- 🔢 **Three units** — percent, Mbit/s, MB/s (with correct conversion)
- 💾 **Persistence** — rules, settings and the last applied state survive
  container restarts
- 🖥️ **WebUI** — no YAML fiddling, everything in the browser, with inline
  help tooltips

## How it works

```
Jellyfin /Sessions ──▶ ┌─────────────────────────┐
SABnzbd  queue     ──▶ │  Resolver (every 15 s)  │ ──▶ SABnzbd speedlimit API
JDownloader state  ──▶ │  per-target time rules  │ ──▶ JDownloader remote API
rules.json         ──▶ │  stream budget split    │
settings.json      ──▶ └─────────────────────────┘
```

Every tick, each downloader's limits are resolved independently:

1. **Base** — that downloader's own time rules (lowest active rule wins)
   or its default limit.
2. **Stream budget** — while Jellyfin streams, the global budget
   (`stream budget %` of the line speed) is split among the downloaders
   that are *actually downloading*. Each downloader gets
   `min(base, budget share)` — the budget tightens, never loosens.

With only one downloader active, it simply gets the whole budget — the
splitting logic only ever matters in the (rare) case that both download at
the same time. Activity is detected from the download queues with a short
hysteresis (2 ticks) so captcha waits or reconnects don't cause limit
flapping. Measured speeds are smoothed with an exponential moving average,
and each share includes headroom so a downloader can ramp up when its host
gets faster.

## Installation

### Unraid (recommended)

Template via *Add Container → Template URL*:

```
https://raw.githubusercontent.com/cosmicflow2512/sabthrottle/main/cosmicflow2512/sabthrottle.xml
```

Fill in the required fields (SABNZBD_URL, SABNZBD_API_KEY, optionally
Jellyfin and MyJDownloader) and Apply. The container persists to
`/mnt/user/appdata/sabthrottle`.

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
      # Optional: JDownloader 2 via MyJDownloader
      MYJD_EMAIL: "you@example.com"
      MYJD_PASSWORD: "your_myjd_password"
      MYJD_DEVICE: "your-jd-device-name"
```

## Usage

After starting: `http://<host>:6811/`

- **Status** — one card per downloader: current limit, reason
  (rule / budget share / default), live activity and speed, and the full
  candidate evaluation of the current tick
- **Rules** — separate tabs for SABnzbd and JDownloader, CRUD for rules;
  each rule has: enabled/disabled, name, weekdays, start/end time, and a
  mode (percent · Mbit/s · MB/s · pause)
- **Settings** — line speed, stream budget, split mode, burst protection,
  per-downloader defaults; every option has an inline ⓘ tooltip

### Example rules

Throttle SABnzbd to 30 Mbit/s on weekday daytimes:

- Tab: SABnzbd · Days: Mon–Fri · Start 07:00, End 17:00 · Mode Mbit/s, value 30

Pause JDownloader during the day (e.g. to save premium host quota):

- Tab: JDownloader · Days: all · Start 08:00, End 20:00 · Mode Pause

Full speed at night (across midnight works automatically):

- Start 22:00, End 06:00 · Mode percent, value 100

## Configuration (environment variables)

Most values are only *seeds* — on first start they populate
`settings.json`; after that the WebUI is the source of truth.

| Variable                     | Required | Default       | Description |
| ---------------------------- | -------- | ------------- | ----------- |
| `SABNZBD_URL`                | ✓        | —             | Base URL of your SABnzbd instance |
| `SABNZBD_API_KEY`            | ✓        | —             | SABnzbd API key |
| `JELLYFIN_URL`               |          | —             | Enables Jellyfin polling |
| `JELLYFIN_API_KEY`           |          | —             | Jellyfin API key (Dashboard → API Keys) |
| `JELLYFIN_POLL_INTERVAL_SEC` |          | `15`          | Poll interval |
| `MYJD_EMAIL`                 |          | —             | MyJDownloader account e-mail (enables the JD target) |
| `MYJD_PASSWORD`              |          | —             | MyJDownloader account password |
| `MYJD_DEVICE`                |          | —             | JDownloader device name as shown on my.jdownloader.org |
| `MYJD_APP_KEY`               |          | `sabthrottle` | App identifier for the MyJD API |
| `STREAM_BUDGET_PERCENT`      |          | `50`          | Seed: total download budget while streaming |
| `FULL_PERCENT`               |          | `100`         | Seed: SABnzbd default limit |
| `LINE_SPEED_MBIT`            |          | `0`           | Seed: real line speed |
| `CONFIG_DIR`                 |          | `/config`     | Where rules.json / settings.json / state.json live |
| `NZBDAV_PATH_PREFIX`         |          | —             | Filter: only throttle when the playing item's path contains this |
| `WEBHOOK_TOKEN`              |          | —             | Shared secret for webhook mode |
| `LISTEN_PORT`                |          | `6811`        | WebUI/API port |
| `LOG_LEVEL`                  |          | `INFO`        | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

`THROTTLE_PERCENT` (v1) is still accepted and maps onto
`STREAM_BUDGET_PERCENT`.

### Upgrading from v1

Nothing to do. On first start, the existing `rules.json` (a flat list) is
migrated automatically: all v1 rules become SABnzbd rules, and the old
`throttle_percent` / `default_percent` settings are renamed. Behaviour with
only SABnzbd configured is identical to v1.

## Endpoints

| Path              | Method     | Purpose |
| ----------------- | ---------- | ------- |
| `/`               | GET        | Status dashboard (HTML) |
| `/rules`          | GET        | Rule overview (HTML, `?target=sab\|jd`) |
| `/settings`       | GET        | Settings (HTML) |
| `/api/status`     | GET        | JSON snapshot incl. per-target decisions |
| `/api/rules`      | GET/POST   | List / add rules (`?target=sab\|jd`, default `sab`) |
| `/api/rules/<id>` | PUT/DELETE | Update / delete a rule (id is searched across targets) |
| `/api/settings`   | GET/POST   | Read / save settings |
| `/jellyfin`       | POST       | Optional: webhook receiver |
| `/health`         | GET        | Healthcheck |

## Troubleshooting

- **SABnzbd limit doesn't change**: check the API key, `SABNZBD_URL`
  without trailing `/`, and that the container can reach SABnzbd (same
  Docker network or IP).
- **JDownloader limit doesn't change**: verify the device name matches
  exactly what my.jdownloader.org shows, and check the container log —
  connection problems and session expiries are logged and retried with a
  60 s cooldown.
- **Stream not detected**: set `LOG_LEVEL=DEBUG`, call `/api/status`
  during playback, and verify the Jellyfin API key.
- **Percent rule and Mbit rule give unexpected results**: set the line
  speed in Settings, otherwise the resolver assumes 1 Gbit/s (or
  SABnzbd's `bandwidth_max`, if set) as the percent reference.
- **Limits flap between two values while streaming**: increase the speed
  smoothing (lower α) in the advanced split tuning, or switch the split
  mode to "equal".

## License

MIT — see [LICENSE](LICENSE).
