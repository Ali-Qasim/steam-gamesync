# gamesync

Automatically add the games in your folders to Steam as non-Steam shortcuts —
with artwork — and keep them in sync. Drop a game into a watched folder and it
shows up in your Steam library; delete the folder and the shortcut disappears.

A lightweight, self-hosted replacement for manually importing games with
[BoilR](https://github.com/PhilipK/BoilR) every time, with artwork handled by
[steamgrid](https://github.com/boppreh/steamgrid) + [SteamGridDB](https://www.steamgriddb.com/).

## What it does

- Watches one or more game folders (e.g. `C:\Games`, `D:\Games`).
- For each top-level game folder, creates **one** Steam shortcut pointing at the
  largest plausible `.exe` (installers, redists and crash handlers are skipped).
- Downloads cover / hero / logo / banner artwork via steamgrid.
- Removes shortcuts when their folder is deleted.
- Runs as a **hidden background watcher** (via `pythonw.exe`), started at logon
  by a Windows scheduled task.
- Tags everything it creates into a Steam category (default **GameSync**) so your
  auto-added games are easy to find.
- **Optional:** mirrors the same games into an [Apollo/Vibepollo](https://github.com/ClassicOldSong/Apollo)
  (Sunshine fork) `apps.json` as launch tiles — with cover art — and hot-reloads
  the app list so they appear on your streaming client without a restart.

## Requirements

- Windows + Python 3.9+
- [steamgrid](https://github.com/boppreh/steamgrid/releases) (a single `.exe`)
- A free [SteamGridDB API key](https://www.steamgriddb.com/profile/preferences)
  (optional, but artwork is skipped without it)

## Install

```sh
git clone https://github.com/<you>/gamesync.git
cd gamesync
python install.py
```

`install.py` is interactive: it asks for your game folders, API key and
steamgrid path, writes `.env`, installs dependencies, registers the logon task,
and starts the watcher. Re-run it any time to change settings.

### Manual install

```sh
pip install -r requirements.txt
cp .env.example .env   # then edit .env
python gamesync.py --once   # initial sync
```

## Usage

| Command | What it does |
| --- | --- |
| `python gamesync.py --once` | Reconcile once and exit |
| `python gamesync.py --once --dry-run` | Show what would change, write nothing |
| `python gamesync.py --watch` | Run the watcher in the foreground |
| `python gamesync.py --art` | Run the artwork pass only |
| `python gamesync.py --status` | List managed shortcuts + artwork state |
| `python uninstall.py` | Stop the watcher and remove the task |

## Configuration

All settings live in `.env` (copy from `.env.example`). Key ones:

| Variable | Meaning |
| --- | --- |
| `GAMES_DIRS` | Comma-separated folders to watch |
| `STEAMGRIDDB_KEY` | SteamGridDB API key (artwork) |
| `STEAMGRID_EXE` | Path to `steamgrid.exe` |
| `STEAM_PATH` | Steam install (blank = auto-detect) |
| `USERDATA_ID` | Steam account id (`auto` = most recent login) |
| `STEAM_TAG` | Steam category for managed shortcuts |
| `NOTIFICATIONS` | Desktop toast on add/remove (`true`/`false`) |
| `RESTART_STEAM_WHEN_IDLE` | Restart Steam to apply changes when idle |
| `APOLLO_SYNC` | Mirror managed games to Apollo/Vibepollo tiles (`true`/`false`) |
| `APOLLO_APPS` | Path to Apollo `apps.json` (blank = auto-detect) |
| `APOLLO_URL` | Apollo web UI base URL for live reload |
| `APOLLO_REORDER_TOKEN` | Scoped API token for no-restart reload |

### Name overrides

steamgrid matches artwork by the shortcut name. If a folder is named awkwardly
(e.g. `FH6`), map it to a real title in `names.json` (copy from
`names.json.example`):

```json
{ "FH6": "Forza Horizon 6" }
```

## How shortcuts are managed

- Shortcuts gamesync creates are tagged `DevkitGameID="gamesync"` ("managed").
- **Dedup:** if a folder has several shortcuts, one is kept. A **manual**
  (untagged) shortcut always wins over a managed one — so if you hand-edit a
  game's shortcut in Steam, gamesync keeps yours and drops its own.
- **Removal:** a shortcut is removed only when its target folder no longer
  exists on disk.

Steam rewrites `shortcuts.vdf` from memory when it exits, so gamesync edits the
file only while Steam is closed — it shuts Steam down (if no game is running),
writes, and relaunches. A backup is saved to `shortcuts.vdf.gamesync.bak`.

## Apollo / Vibepollo launch tiles (optional)

If you stream your games with [Apollo](https://github.com/ClassicOldSong/Apollo)
or [Vibepollo](https://github.com/Nonary/Vibepollo) (Sunshine forks), gamesync can
also add each managed game as a launch tile so it shows up in your
Artemis/Moonlight client.

- Enabled by `APOLLO_SYNC=true`. It **auto-skips** if no `apps.json` is found, so
  it's a harmless no-op on machines without Apollo — safe to leave on everywhere.
- Only gamesync's own tiles are managed (marked `gamesync-managed`, or whose
  `cmd` points inside a tracked games dir). Your **Desktop**, **Steam Big
  Picture** and any manual tiles are never touched. A backup is written to
  `apps.json.gamesync.bak`.
- Tiles reuse the Steam grid **cover art** steamgrid already downloaded, so they
  look the same as your Steam library.

### Live reload without a restart

After writing tiles, gamesync asks Apollo to re-read `apps.json` in-memory — the
same thing the tray **Reload Apps** does — so new games appear on the client
without restarting Apollo or dropping an active stream.

This needs a **scoped API token**:

1. Open the Apollo web UI (default `https://localhost:47990`) → **API Tokens**.
2. Create a token scoped to **`POST /api/apps/reorder`**.
3. Put it in `.env` as `APOLLO_REORDER_TOKEN=...`.

Without a token the tiles are still written; you'd just reload manually via the
Apollo tray. The token is scoped to reordering only, so it can't touch anything
else — but keep it secret (it stays in the gitignored `.env`).

## Notes

- `.env` and `names.json` are gitignored. **Never commit your API key or Apollo token.**
- Linux/macOS aren't supported yet (the watcher/task logic is Windows-only),
  though the shortcut format is cross-platform.

## License

MIT
