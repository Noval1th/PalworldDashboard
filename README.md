# Palworld Server Dashboard

A self-hosted, live web dashboard for a **Palworld dedicated server** (Windows host). It shows who's online,
a day/night clock read from the actual in-game time, server FPS/uptime history, guilds, base camps, a
**Palpedia completion leaderboard**, a trophy case (Lucky/Alpha/top Pal), and deep per-player and per-Pal
detail — with click-through popups and optional new-connection alerts. Everything is pulled from the Palworld
REST API and the world save. No third-party services, no external APIs.

The page is a single static HTML file that reads one JSON file. A background collector regenerates that JSON
every minute. You can serve it from the game box itself, or push it to any web host.

---

## What it shows

- **Live status** — online players, peak online, server FPS (with a 24h sparkline), uptime %, base camps.
- **Day/night clock** — the *exact* in-game time and day, read from the save (`GameDateTimeTicks`). Correct
  through sleep-skips; no guessing.
- **Tamers roster** — persistent: players who log off stay listed with last-seen (hover for the exact
  timestamp), total playtime, sessions, level, guild, and distance travelled. Online players show first,
  with ping. **Sortable** by any column; **click a tamer** for a full deep-dive.
- **Deep-dive popups** — click any tamer, guild, or Pal for a detail modal (all cross-linked): a tamer's
  exploration / conquest / lifetime stats plus their own notable Pals; a guild's combined member totals; a
  Pal's IV breakdown, gender, soul upgrades, equipped moves, bond, favourite/nickname, and caught-time.
- **Palpedia leaderboard** — per-player species-discovered count (the true Palpedia — species *ever* found,
  from each player's save), ranked, with completion bars.
- **Top Pals** — a ranked showcase (by level / IVs / Lucky / Alpha / nicknamed), with favourited and
  nicknamed Pals emphasised and an on-page legend for the ⭐ / ✨ / 💀 / gender badges.
- **Trophy case** — Lucky (shiny) Pals, Alphas, and the highest-level Pal on the server (with owner).
- **Guilds** — name, base level, member list, Pal counts, and combined member progress.
- **Server & world history** — FPS and world-save-size sparklines over 24h/7d, world playtime.
- **Connection alerts** (opt-in) — a bell toggle that pings (sound + on-page toast + desktop notification)
  when a tamer connects, so you know someone joined without watching the page. Off by default (it's a public
  page), remembered per browser.

Everything on the page is safe to make public: **no Steam IDs, IPs, or map coordinates are ever written to
the published JSON** (see [Privacy](#privacy)).

---

## How it works

```
Palworld dedicated server (Windows)
        │  REST API (localhost:8212)      ┌─ pal-dashboard-collector.ps1  (every 1 min)
        │  world save (Level.sav, *.sav)  │     • polls REST: players, metrics, settings
        └────────────────────────────────►│     • reads exact in-game clock via pal-gametime.py
                                           │     • keeps a private store (roster/history/coords)
                                           │     • writes  webDir/palworld.json   ◄── PUBLIC
                                           │
                                           └─ pal-save-parse.py            (every ~15 min)
                                                 • parses Level.sav + Players/*.sav
                                                 • writes dataDir/palworld-save.json (guilds, Palpedia, …)
                                                 • collector merges it into palworld.json

web/index.html  ──fetch('./palworld.json')──►  renders the dashboard, refreshes every 20s
```

- **`dataDir`** holds the private store (Steam IDs, coordinates) and the parser's intermediate file. **Never
  serve this folder.**
- **`webDir`** holds `index.html` + `palworld.json`. **This is the only thing you serve/publish.**

The save parser exists because the REST API is thin — it exposes players/metrics/settings but *no* world
detail (guilds, Pals, Palpedia) and *no* time-of-day. Those come from parsing the save.

---

## Requirements

- A **Windows** machine running the Palworld dedicated server (the collector reads the save files directly,
  so it must run on the same machine, or one with direct file access to the save folder).
- **Admin PowerShell** for install (to register scheduled tasks and download the bundled Python).
- Internet access during install (downloads an embeddable Python + two Python packages). Nothing else phones
  home afterward.
- ~150 MB of disk for the bundled Python.

No system-wide Python needed — the installer drops a self-contained Python into `collector/python/` and never
touches your PATH or registry.

---

## Setup

### 1. Enable the Palworld REST API

In your `PalWorldSettings.ini` (`...\Pal\Saved\Config\WindowsServer\PalWorldSettings.ini`), inside the single
`OptionSettings=(...)` line, set:

```
RESTAPIEnabled=True,
RESTAPIPort=8212,
AdminPassword="something-strong",
```

Restart the server (config is read only at boot). The collector reads the port and `AdminPassword` **directly
from this ini** at runtime — you never copy the password into this project.

> **Security:** the REST API is an authenticated admin interface. Keep port **8212 firewalled to localhost /
> LAN only** — do **not** port-forward it to the internet. The collector talks to it over `127.0.0.1`.

### 2. Configure

```powershell
cd palworld-dashboard\collector
copy config.example.json config.json
notepad config.json
```

| key | what it is |
|-----|-----------|
| `palworldSaveRoot` | the `SaveGames\0` folder (contains a `<world-id>\Level.sav`). The world id is auto-detected, so a world wipe won't break anything. |
| `palworldConfigIni` | full path to `PalWorldSettings.ini` (used to read the REST port + admin password). |
| `restHost` | usually `127.0.0.1`. |
| `dataDir` | private working folder (store + coords). **Not** web-served. |
| `webDir` | folder that gets served to the web — `index.html` + `palworld.json` land here. |
| `speciesTotal` | Palpedia size for the completion %, for your game version (the leaderboard ranks on raw count regardless). |
| `publishCommand` | optional; see [Publishing](#publishing). Leave `""` to serve `webDir` locally. |

### 3. Install

In an **admin** PowerShell:

```powershell
cd palworld-dashboard
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

This installs the bundled Python + save libraries, copies `index.html` into your `webDir`, and registers two
scheduled tasks (**Palworld Dashboard Collector** every 1 min, **Palworld Save Parser** every 15 min), running
as `SYSTEM`. Within a minute, `webDir\palworld.json` appears.

### 4. Serve it

`webDir` is just static files. Any static web server works. Quick local test:

```powershell
cd C:\PalDashboard\web      # your webDir
python -m http.server 8080  # or: npx serve, IIS, nginx, Caddy, ...
```

Then open `http://localhost:8080`. For a public dashboard, serve `webDir` behind whatever you already use, or
push it to a host — see below.

---

## Publishing (optional)

If the game box can't be reached from the internet (home NAT, etc.), push `webDir` to a machine that can.
Set `publishCommand` in `config.json` to any PowerShell command — it runs after each collector cycle. Example
(push the JSON to a VPS with scp; `$out` and `$webDir` are in scope):

```json
"publishCommand": "scp -i C:\\path\\to\\key -o BatchMode=yes $out user@example.com:/var/www/dashboard/palworld.json"
```

`index.html` only needs to be uploaded once (it fetches `./palworld.json` relatively, so host both at the same
path level). If you serve `webDir` directly, leave `publishCommand` as `""`.

---

## Privacy

The dashboard is designed to be publicly shareable:

- The collector keeps a **private store** in `dataDir` keyed on Steam `userId`, holding raw world coordinates
  (used only to compute distance-travelled). **This file is never published.**
- The published `palworld.json` contains only display names, levels, playtime, ping, and aggregate counts —
  **no Steam IDs, no IP addresses, no coordinates.**
- Player names *are* shown (that's the point of a roster/leaderboard). If you don't want that, don't publish.
- Sensitive server settings (admin/RCON passwords, ports, ban-list URL) are stripped from the settings block.

Only ever serve/publish `webDir`. Keep `dataDir` local.

---

## Files

```
install.ps1                         one-shot installer (admin)
collector/
  pal-dashboard-collector.ps1       every 1 min: REST + clock -> palworld.json
  pal-save-parse.py                 every 15 min: Level.sav + player saves -> palworld-save.json
  pal-gametime.py                   reads the exact in-game clock (called by the collector)
  pal-names.json                    internal CharacterID -> display name (base names + variant suffixes)
  config.example.json               copy to config.json and edit
web/
  index.html                        the dashboard (static; fetches ./palworld.json)
```

### Pal names

`pal-names.json` maps Palworld's internal `CharacterID`s (e.g. `PinkCat`) to display names (`Cattiva`). Only
**base** names are stored — variants are derived from the suffix (`SheepBall_Ice` -> `Lamball Cryst`) and
`BOSS_` -> `Alpha …`. Unknown species fall back to their internal ID (shown in muted italics), so a missing
entry is cosmetic, never a crash. Dedicated-server files ship internal IDs only, so this table is maintained
by hand; add a line for any species that shows up unmapped.

---

## Under the hood: reading the save

Palworld 1.0 saves use the **`PlM`** container (Oodle/Kraken compressed). The standard `palworld-save-tools`
only handles the older `PlZ` (zlib) format, so this project decompresses with **`pyooz`** (an open-source
Kraken decoder — no proprietary Oodle DLL needed) and hands the raw GVAS to the parser. Two 1.0 struct changes
(a grown character record, and inserted fields in the guild struct) are worked around in `pal-save-parse.py`.
See the comments there. The in-game clock comes from `worldSaveData.GameTimeSaveData.GameDateTimeTicks`
(`floor(ticks / 864000000000)` = day; the remainder = time of day).

If a future Palworld patch changes the save format, the save-derived panels (guilds/Palpedia/etc.) may go stale
— the collector ignores `palworld-save.json` once it's >6h old, so the dashboard **degrades gracefully**: the
REST-driven parts keep working, the save-derived panels just disappear.

---

## Troubleshooting

- **`palworld.json` never appears** — check the **Palworld Dashboard Collector** task ran (Task Scheduler →
  History). Run it by hand: `powershell -ExecutionPolicy Bypass -File collector\pal-dashboard-collector.ps1`.
- **Everything shows offline / empty** — the REST API isn't reachable. Confirm `RESTAPIEnabled=True`, the
  server was restarted after editing the ini, and the port/host in `config.json` match.
- **Guilds/Palpedia/leaderboard missing** — that's the save parser. Run it by hand:
  `collector\python\python.exe collector\pal-save-parse.py` and read any error. A Palworld save-format change
  is the usual cause.
- **Clock says the wrong time** — it's read straight from the save, so it should be exact. If it's blank, the
  save read is failing (see the parser troubleshooting above).

---

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Pocketpair. "Palworld" is a trademark of its owner.
Built on [`palworld-save-tools`](https://github.com/cheahjs/palworld-save-tools) and `pyooz`.
