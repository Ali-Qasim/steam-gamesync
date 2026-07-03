#!/usr/bin/env python3
"""
gamesync - keep Steam non-Steam shortcuts in sync with your game folders,
fetch artwork via steamgrid, and run as a hidden background watcher.

Modes:
  python gamesync.py --once            reconcile once, then exit
  python gamesync.py --once --dry-run  show what would change, write nothing
  python gamesync.py --watch           run forever (file watcher + periodic check)
  python gamesync.py --art             run the artwork pass only
  python gamesync.py --status          list managed shortcuts and artwork state

Configuration lives in a local .env file (see .env.example). Run install.py
for an interactive first-time setup.

Rules:
  - One Steam shortcut per top-level folder under each tracked games dir.
  - Entries the script creates/adopts are tagged managed (DevkitGameID="gamesync").
  - Dedup: if a folder has several shortcuts, keep one:
        * if a manual (untagged) entry exists alongside a managed one -> keep the
          MANUAL one (you added it on purpose), drop the managed one;
        * otherwise prefer the "boilr-style" entry (StartDir == bare folder) and
          tag it managed.
  - Remove: any shortcut pointing into a folder that no longer exists on disk.
  - Add: any folder with no shortcut and a detectable game exe -> create managed.
"""

import sys
import os
import re
import json
import zlib
import time
import threading
import subprocess
import logging
from logging.handlers import RotatingFileHandler

import vdf

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
NAMES_PATH = os.path.join(HERE, "names.json")
LOG_PATH = os.path.join(HERE, "gamesync.log")
MARKER = "gamesync"  # stored in DevkitGameID to mark script-managed shortcuts

_lock = threading.RLock()

DEFAULTS = {
    "skip_folders": ["_CommonRedist", ".claude"],
    "exe_skip_pattern": (
        r"(?i)(unins|setup|redist|vcredist|vc_redist|directx|dxsetup|crash|report|"
        r"unitycrash|crashreport|vbsp|vrad|vvis|notification_helper|dotnet|oalinst|"
        r"touchup|prereq|easyanticheat|battleye|launcher_installer)"
    ),
    "name_strip_pattern": (
        r"(?i)[-_. ]*(steamrip\.com|steamrip|fitgirl|repack|dodi|codex|plaza|"
        r"skidrow|tenoke|empress|razor1911)\b.*$"
    ),
    "steamgrid_timeout_seconds": 300,
    "debounce_seconds": 45,
    "periodic_check_minutes": 3,
    "restart_steam_when_idle": True,
    "steam_tag": "GameSync",
    "notifications": False,
}


# ----------------------------------------------------------------------------- logging
logger = logging.getLogger("gamesync")


def setup_logging():
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)


def log(msg):
    logger.info(msg)


# ----------------------------------------------------------------------------- config
def _bool(v, default=False):
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def detect_steam_path():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            p, _ = winreg.QueryValueEx(k, "SteamPath")
            if p and os.path.isdir(p):
                return os.path.normpath(p)
    except (OSError, ImportError):
        pass
    for guess in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        if os.path.isdir(guess):
            return guess
    return ""


def detect_apollo_apps():
    """Locate Apollo/Vibepollo apps.json (Sunshine-fork launch tiles)."""
    for guess in (
        r"C:\Program Files\Apollo\config\apps.json",
        r"C:\Program Files\Vibepollo\config\apps.json",
        r"C:\Program Files (x86)\Apollo\config\apps.json",
        r"C:\Program Files (x86)\Vibepollo\config\apps.json",
    ):
        if os.path.isfile(guess):
            return guess
    return ""


def load_name_overrides():
    if not os.path.isfile(NAMES_PATH):
        return {}
    try:
        with open(NAMES_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        return {str(k).lower(): str(v) for k, v in raw.items()}
    except (OSError, ValueError):
        return {}


def load_config():
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
    except ImportError:
        pass
    g = os.environ.get
    cfg = dict(DEFAULTS)

    dirs = g("GAMES_DIRS", "")
    cfg["games_dirs"] = [d.strip() for d in dirs.split(",") if d.strip()]
    cfg["steam_path"] = (g("STEAM_PATH", "") or "").strip() or detect_steam_path()
    cfg["userdata_id"] = (g("USERDATA_ID", "auto") or "auto").strip() or "auto"
    cfg["steamgrid_exe"] = (g("STEAMGRID_EXE", "") or "").strip()
    cfg["steamgriddb_key"] = (g("STEAMGRIDDB_KEY", "") or "").strip()

    if g("SKIP_FOLDERS"):
        cfg["skip_folders"] = [s.strip() for s in g("SKIP_FOLDERS").split(",") if s.strip()]
    cfg["debounce_seconds"] = int(g("DEBOUNCE_SECONDS") or cfg["debounce_seconds"])
    cfg["periodic_check_minutes"] = int(g("PERIODIC_CHECK_MINUTES") or cfg["periodic_check_minutes"])
    cfg["steamgrid_timeout_seconds"] = int(g("STEAMGRID_TIMEOUT_SECONDS") or cfg["steamgrid_timeout_seconds"])
    cfg["restart_steam_when_idle"] = _bool(g("RESTART_STEAM_WHEN_IDLE"), True)
    cfg["steam_tag"] = (g("STEAM_TAG") or cfg["steam_tag"]).strip()
    cfg["notifications"] = _bool(g("NOTIFICATIONS"), False)
    cfg["name_overrides"] = load_name_overrides()

    cfg["apollo_sync"] = _bool(g("APOLLO_SYNC"), True)
    cfg["apollo_apps"] = (g("APOLLO_APPS", "") or "").strip() or detect_apollo_apps()
    cfg["apollo_url"] = (g("APOLLO_URL", "") or "").strip() or "https://localhost:47990"
    cfg["apollo_token"] = (g("APOLLO_REORDER_TOKEN", "") or g("APOLLO_TOKEN", "") or "").strip()

    if not cfg["games_dirs"]:
        raise RuntimeError("GAMES_DIRS is empty. Set it in .env (run install.py).")
    if not cfg["steam_path"]:
        raise RuntimeError("Steam path not found. Set STEAM_PATH in .env (run install.py).")
    return cfg


# ----------------------------------------------------------------------------- helpers
def cft(e, key, default=""):
    """Case-insensitive get from a shortcut entry dict."""
    kl = key.lower()
    for k, v in e.items():
        if k.lower() == kl:
            return v
    return default


def strip_quotes(s):
    return str(s).strip().strip('"').strip()


def resolve_userdata(cfg):
    if cfg.get("userdata_id") and cfg["userdata_id"] != "auto":
        return str(cfg["userdata_id"])
    lu = os.path.join(cfg["steam_path"], "config", "loginusers.vdf")
    best, best_ts = None, -1
    try:
        with open(lu, encoding="utf-8") as f:
            users = vdf.load(f).get("users", {})
        for sid64, info in users.items():
            if info.get("MostRecent") == "1":
                best = sid64
                break
            ts = int(info.get("Timestamp", 0) or 0)
            if ts > best_ts:
                best, best_ts = sid64, ts
    except (OSError, ValueError):
        pass
    if best:
        return str(int(best) - 76561197960265728)
    ud = os.path.join(cfg["steam_path"], "userdata")
    dirs = [d for d in os.listdir(ud) if d.isdigit()]
    if len(dirs) == 1:
        return dirs[0]
    raise RuntimeError("Could not resolve Steam userdata account; set USERDATA_ID in .env")


def shortcuts_path(cfg, uid):
    return os.path.join(cfg["steam_path"], "userdata", uid, "config", "shortcuts.vdf")


def grid_dir(cfg, uid):
    return os.path.join(cfg["steam_path"], "userdata", uid, "config", "grid")


def shortcut_appid(exe, appname):
    """Deterministic 32-bit signed appid (same scheme steamgrid/SRM use)."""
    key = (exe + appname).encode("utf-8")
    top = (zlib.crc32(key) & 0xFFFFFFFF) | 0x80000000
    return top - 0x100000000  # to signed 32-bit


# ----------------------------------------------------------------------------- exe / name
def find_exe(folder, skip_re):
    best, best_size = None, -1
    for root, _dirs, files in os.walk(folder):
        for fn in files:
            if not fn.lower().endswith(".exe"):
                continue
            if skip_re.search(fn):
                continue
            p = os.path.join(root, fn)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            if sz > best_size:
                best, best_size = p, sz
    return best


def clean_name(folder_name, strip_re, overrides):
    ov = overrides.get(folder_name.lower())
    if ov:
        return ov
    name = strip_re.sub("", folder_name)
    if "." in name and " " not in name:
        name = name.replace(".", " ")
    name = name.replace("_", " ").strip(" -_.")
    return name or folder_name


# ----------------------------------------------------------------------------- folder mapping
def games_dirs(cfg):
    return list(cfg.get("games_dirs", []))


def games_roots(cfg):
    return [os.path.normcase(os.path.abspath(d)) for d in games_dirs(cfg)]


def top_folder_of(path, roots):
    """Return the full normcased top-level folder path if `path` is under any
    tracked games dir, else None."""
    p = os.path.normcase(os.path.abspath(strip_quotes(path)))
    for root in roots:
        prefix = root + os.sep
        if p.startswith(prefix):
            top = p[len(prefix):].split(os.sep)[0]
            return os.path.join(root, top)
    return None


def entry_top(e, roots):
    return top_folder_of(cft(e, "StartDir"), roots) or top_folder_of(cft(e, "Exe"), roots)


def is_managed(e):
    return str(cft(e, "DevkitGameID")).lower() == MARKER


def is_boilr_style(e, roots):
    raw = strip_quotes(cft(e, "StartDir"))
    if raw.endswith(("\\", "/")):
        return False
    top = top_folder_of(raw, roots)
    if not top:
        return False
    return os.path.normcase(os.path.abspath(raw)) == top


def exe_exists(e):
    try:
        return os.path.isfile(strip_quotes(cft(e, "Exe")))
    except OSError:
        return False


def make_entry(folder_real, exe, name, tag):
    exe_q = f'"{exe}"'
    start_q = f'"{folder_real}"'
    tags = {"0": tag} if tag else {}
    return {
        "appid": shortcut_appid(exe_q, name),
        "AppName": name,
        "Exe": exe_q,
        "StartDir": start_q,
        "icon": "",
        "ShortcutPath": "",
        "LaunchOptions": "",
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitGameID": MARKER,
        "DevkitOverrideAppID": 0,
        "LastPlayTime": 0,
        "FlatpakAppID": "",
        "tags": tags,
    }


# ----------------------------------------------------------------------------- notifications
def notify(title, msg, cfg):
    if not cfg.get("notifications"):
        return
    try:
        from windows_toasts import Toast, WindowsToaster
        toaster = WindowsToaster("GameSync")
        toast = Toast()
        toast.text_fields = [title, msg]
        toaster.show_toast(toast)
    except Exception:
        pass  # notifications are best-effort only


# ----------------------------------------------------------------------------- steam process
def steam_running():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"],
            capture_output=True, text=True, timeout=20,
        ).stdout.lower()
        return "steam.exe" in out
    except (OSError, subprocess.SubprocessError):
        return False


def game_running():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            val, _ = winreg.QueryValueEx(k, "RunningAppID")
            return int(val) != 0
    except (OSError, ValueError, ImportError):
        return False


def shutdown_steam(cfg):
    steam_exe = os.path.join(cfg["steam_path"], "steam.exe")
    log("Shutting down Steam to apply changes...")
    try:
        subprocess.run([steam_exe, "-shutdown"], timeout=20)
    except (OSError, subprocess.SubprocessError) as ex:
        log(f"  steam -shutdown error: {ex}")
    for _ in range(40):
        if not steam_running():
            log("  Steam closed.")
            return True
        time.sleep(1)
    log("  WARNING: Steam did not close in time.")
    return False


def launch_steam(cfg):
    steam_exe = os.path.join(cfg["steam_path"], "steam.exe")
    try:
        subprocess.Popen([steam_exe], close_fds=True)
        log("Relaunched Steam.")
    except OSError as ex:
        log(f"  Could not relaunch Steam: {ex}")


def run_steamgrid(cfg):
    sg = cfg.get("steamgrid_exe")
    if not sg or not os.path.isfile(sg):
        log("steamgrid.exe not configured/found; skipping artwork.")
        return
    key = cfg.get("steamgriddb_key")
    if not key:
        log("No STEAMGRIDDB_KEY set; skipping artwork (steamgrid hangs without a key).")
        return
    args = [sg, "-nonsteamonly", "-onlymissingartwork",
            "-steamdir", cfg["steam_path"], "-steamgriddb", key]
    timeout = int(cfg.get("steamgrid_timeout_seconds", 300))
    log("Running steamgrid for artwork...")
    try:
        # steamgrid ends with a blocking "Press enter" prompt; DEVNULL gives it
        # EOF so the read returns and it exits cleanly.
        r = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            cwd=os.path.dirname(sg),
        )
        for ln in (r.stdout or "").splitlines():
            ln = ln.rstrip()
            if ln and "press enter" not in ln.lower():
                log(f"  steamgrid | {ln}")
        log(f"  steamgrid finished (exit {r.returncode}).")
    except subprocess.TimeoutExpired:
        log(f"  steamgrid timed out after {timeout}s.")
    except (OSError, subprocess.SubprocessError) as ex:
        log(f"  steamgrid error: {ex}")


# ----------------------------------------------------------------------------- apollo tiles
def _apollo_uuid(exe):
    """Deterministic uppercase GUID per game exe, so re-runs are stable."""
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "gamesync:" + os.path.normcase(exe))).upper()


def _grid_cover(grid, unsigned):
    """Best cover art file in the Steam grid dir for a managed appid.
    Prefer the portrait cover (<appid>p.png); avoid hero/logo."""
    if not os.path.isdir(grid):
        return ""
    cands = [fn for fn in os.listdir(grid) if fn.startswith(str(unsigned))]

    def rank(fn):
        low = fn.lower()
        if low.endswith(("p.png", "p.jpg")):
            return 0
        if "_hero" in low or "_logo" in low:
            return 3
        if low.endswith((".png", ".jpg")):
            return 1
        return 2

    cands.sort(key=rank)
    return os.path.join(grid, cands[0]) if cands else ""


def reload_apollo(cfg):
    """Force Apollo/Vibepollo to re-read apps.json in-memory WITHOUT a restart -
    equivalent to the tray 'Reload Apps'. Uses POST /api/apps/reorder with an
    empty order (lossless no-op: all apps are re-appended unchanged) which the
    server follows with proc::refresh. Reloads only the app list, not running
    streams, so it's safe to call mid-session."""
    token = cfg.get("apollo_token")
    if not token:
        log("APOLLO_TOKEN not set - wrote apps.json but skipped live reload "
            "(use Apollo tray 'Reload Apps' or set a token in .env).")
        return
    url = cfg.get("apollo_url", "https://localhost:47990").rstrip("/") + "/api/apps/reorder"
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        r = requests.post(
            url, json={"order": []},
            headers={"Authorization": "Bearer " + token},
            verify=False, timeout=15,
        )
        if r.status_code == 200:
            log("Apollo app list reloaded (no restart).")
        else:
            log(f"Apollo reload failed: HTTP {r.status_code} {r.text[:200]}")
    except Exception as ex:  # network/tls/etc - best-effort, never fatal
        log(f"Apollo reload error: {ex}")


def sync_apollo(cfg, kept, uid, dry=False):
    """Mirror gamesync-managed shortcuts into Apollo/Vibepollo apps.json as
    launch tiles. Only touches entries we own (marked gamesync-managed, or whose
    cmd points inside a tracked games dir); user/preset tiles are left intact."""
    if not cfg.get("apollo_sync", True):
        return
    path = cfg.get("apollo_apps")
    if not path:
        return
    if not os.path.isfile(path):
        log(f"Apollo apps.json not found ({path}); skipping tile sync.")
        return

    roots = games_roots(cfg)
    grid = grid_dir(cfg, uid)

    desired = {}
    for e in kept:
        if not is_managed(e):
            continue
        exe = strip_quotes(cft(e, "Exe"))
        if not exe:
            continue
        name = cft(e, "AppName")
        startdir = strip_quotes(cft(e, "StartDir")) or os.path.dirname(exe)
        appid = cft(e, "appid", 0)
        unsigned = appid & 0xFFFFFFFF if isinstance(appid, int) else 0
        entry = {
            "name": name,
            "cmd": exe,
            "working-dir": startdir,
            "auto-detach": True,
            "wait-all": True,
            "exclude-global-prep-cmd": False,
            "elevated": False,
            "uuid": _apollo_uuid(exe),
            "gamesync-managed": True,
        }
        img = _grid_cover(grid, unsigned)
        if img:
            entry["image-path"] = img
        desired[os.path.normcase(exe)] = entry

    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as ex:
        log(f"Apollo apps.json unreadable ({ex}); skipping tile sync.")
        return

    apps = doc.get("apps", [])

    def is_ours(a):
        if a.get("gamesync-managed"):
            return True
        c = strip_quotes(a.get("cmd", ""))
        return bool(c) and top_folder_of(c, roots) is not None

    others = [a for a in apps if not is_ours(a)]
    new_apps = others + list(desired.values())

    def norm(lst):
        return json.dumps(lst, sort_keys=True)

    if norm(apps) == norm(new_apps):
        return  # already in sync

    log(f"Apollo tiles: {len(desired)} managed game(s), {len(others)} other tile(s).")
    if dry:
        log("  (dry run - apps.json not written)")
        return

    doc["apps"] = new_apps
    try:
        import shutil
        shutil.copy2(path, path + ".gamesync.bak")
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=4)
    log(f"Wrote Apollo apps.json ({len(new_apps)} tiles).")
    reload_apollo(cfg)


# ----------------------------------------------------------------------------- core reconcile
def plan(cfg):
    roots = games_roots(cfg)
    uid = resolve_userdata(cfg)
    sc_path = shortcuts_path(cfg, uid)
    tag = cfg.get("steam_tag", "")

    with open(sc_path, "rb") as f:
        data = vdf.binary_load(f)
    shortcuts = data.get("shortcuts", {})
    entries = list(shortcuts.values())

    skip = {s.lower() for s in cfg.get("skip_folders", [])}
    skip_re = re.compile(cfg["exe_skip_pattern"])
    strip_re = re.compile(cfg["name_strip_pattern"])
    overrides = cfg.get("name_overrides", {})

    disk = {}
    for gdir in games_dirs(cfg):
        if not os.path.isdir(gdir):
            continue
        for name in os.listdir(gdir):
            full = os.path.join(gdir, name)
            if name.startswith("."):
                continue
            if os.path.isdir(full) and name.lower() not in skip:
                disk[os.path.normcase(os.path.abspath(full))] = full

    non_games = [e for e in entries if entry_top(e, roots) is None]
    groups = {}
    for e in entries:
        t = entry_top(e, roots)
        if t is not None:
            groups.setdefault(t, []).append(e)

    kept = list(non_games)
    actions = []

    for top, elist in groups.items():
        if top not in disk:
            for e in elist:
                actions.append(("REMOVE", cft(e, "AppName"), "folder deleted"))
            continue

        managed = [e for e in elist if is_managed(e)]
        manual = [e for e in elist if not is_managed(e)]

        if managed and manual:
            keeper = next((e for e in manual if exe_exists(e)), manual[0])
            for e in elist:
                if e is not keeper:
                    actions.append(("REMOVE", cft(e, "AppName"),
                                    "superseded by manual entry"))
            kept.append(keeper)
        elif managed:
            keeper = next((e for e in managed if exe_exists(e)), managed[0])
            for e in managed:
                if e is not keeper:
                    actions.append(("REMOVE", cft(e, "AppName"), "duplicate"))
            kept.append(keeper)
        else:
            keeper = (next((e for e in manual if is_boilr_style(e, roots)), None)
                      or next((e for e in manual if exe_exists(e)), None)
                      or manual[0])
            for e in manual:
                if e is not keeper:
                    why = "manual dup (kept boilr)" if is_boilr_style(keeper, roots) else "duplicate"
                    actions.append(("REMOVE", cft(e, "AppName"), why))
            if is_boilr_style(keeper, roots) and not is_managed(keeper):
                keeper["DevkitGameID"] = MARKER
                if tag and not cft(keeper, "tags"):
                    keeper["tags"] = {"0": tag}
                actions.append(("ADOPT", cft(keeper, "AppName"), "tagged managed"))
            kept.append(keeper)

    covered = set(groups.keys())
    for top_full, folder_real in disk.items():
        if top_full in covered:
            continue
        exe = find_exe(folder_real, skip_re)
        real = os.path.basename(folder_real)
        if not exe:
            actions.append(("SKIP", real, "no game exe found"))
            continue
        name = clean_name(real, strip_re, overrides)
        kept.append(make_entry(folder_real, exe, name, tag))
        actions.append(("ADD", name, os.path.relpath(exe, folder_real)))

    changed = any(a[0] in ("REMOVE", "ADD", "ADOPT") for a in actions)
    return data, kept, actions, changed, sc_path, uid


def write_shortcuts(data, kept, sc_path):
    data["shortcuts"] = {str(i): e for i, e in enumerate(kept)}
    bak = sc_path + ".gamesync.bak"
    try:
        if os.path.isfile(sc_path):
            import shutil
            shutil.copy2(sc_path, bak)
    except OSError:
        pass
    with open(sc_path, "wb") as f:
        vdf.binary_dump(data, f)


def reconcile(cfg, dry=False):
    with _lock:
        try:
            data, kept, actions, changed, sc_path, uid = plan(cfg)
        except (OSError, KeyError, ValueError, RuntimeError) as ex:
            log(f"ERROR during plan: {ex}")
            return

        if actions:
            for verb, name, detail in actions:
                log(f"  {verb:<7} {name}  ({detail})")
        else:
            log("No changes. Library in sync.")

        if dry:
            if actions:
                log("Dry run - nothing written.")
            sync_apollo(cfg, kept, uid, dry=True)
            return

        if changed:
            running = steam_running()
            if running and game_running():
                log("A game is running - deferring Steam write until idle.")
            else:
                did_shutdown = False
                if running and cfg.get("restart_steam_when_idle", True):
                    did_shutdown = shutdown_steam(cfg)
                if running and cfg.get("restart_steam_when_idle", True) and not did_shutdown:
                    log("Could not close Steam cleanly; deferring Steam write.")
                else:
                    write_shortcuts(data, kept, sc_path)
                    log(f"Wrote shortcuts.vdf ({len(kept)} shortcuts).")
                    for verb, name, _ in actions:
                        if verb == "ADD":
                            notify("Game added to Steam", name, cfg)
                        elif verb == "REMOVE":
                            notify("Shortcut removed", name, cfg)
                    if did_shutdown:
                        launch_steam(cfg)
                    run_steamgrid(cfg)
                    log("Reconcile complete.")

        # Apollo tiles don't depend on Steam being closed - always keep them in sync.
        sync_apollo(cfg, kept, uid)


# ----------------------------------------------------------------------------- status
def status(cfg):
    uid = resolve_userdata(cfg)
    sc_path = shortcuts_path(cfg, uid)
    gdir = grid_dir(cfg, uid)
    grid_files = os.listdir(gdir) if os.path.isdir(gdir) else []
    with open(sc_path, "rb") as f:
        data = vdf.binary_load(f)
    managed = [e for e in data.get("shortcuts", {}).values() if is_managed(e)]
    print(f"Tracked dirs : {', '.join(games_dirs(cfg))}")
    print(f"Steam account: {uid}")
    print(f"Managed shortcuts: {len(managed)}\n")
    for e in sorted(managed, key=lambda x: cft(x, "AppName").lower()):
        appid = cft(e, "appid", 0)
        unsigned = appid & 0xFFFFFFFF if isinstance(appid, int) else 0
        has_art = any(fn.startswith(str(unsigned)) for fn in grid_files)
        art = "art" if has_art else "NO ART"
        print(f"  [{art:>6}] {cft(e, 'AppName')}")
        print(f"            {strip_quotes(cft(e, 'Exe'))}")

    ap = cfg.get("apollo_apps")
    if cfg.get("apollo_sync", True) and ap and os.path.isfile(ap):
        try:
            with open(ap, encoding="utf-8") as f:
                doc = json.load(f)
            roots = games_roots(cfg)
            mine = [a for a in doc.get("apps", [])
                    if a.get("gamesync-managed")
                    or top_folder_of(strip_quotes(a.get("cmd", "")), roots) is not None]
            print(f"\nApollo tiles (managed): {len(mine)}")
            print(f"            {ap}")
        except (OSError, ValueError):
            pass


# ----------------------------------------------------------------------------- watch mode
def watch(cfg):
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    state = {"last_event": 0.0, "pending": False}

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            if not event.is_directory:
                return
            state["last_event"] = time.time()
            state["pending"] = True
            log(f"Detected change: {event.event_type} {event.src_path}")

    obs = Observer()
    watched = []
    for gdir in games_dirs(cfg):
        if os.path.isdir(gdir):
            obs.schedule(Handler(), gdir, recursive=False)
            watched.append(gdir)
        else:
            log(f"WARNING: tracked dir does not exist, skipping: {gdir}")
    obs.start()
    log(f"Watching {watched} (debounce {cfg['debounce_seconds']}s, "
        f"periodic {cfg['periodic_check_minutes']}m)")

    debounce = cfg["debounce_seconds"]
    period = cfg["periodic_check_minutes"] * 60
    last_periodic = time.time()
    reconcile(cfg)
    try:
        while True:
            time.sleep(3)
            now = time.time()
            if state["pending"] and (now - state["last_event"]) >= debounce:
                state["pending"] = False
                log("Debounce elapsed - reconciling.")
                reconcile(cfg)
                last_periodic = now
            elif (now - last_periodic) >= period:
                last_periodic = now
                reconcile(cfg)
    except KeyboardInterrupt:
        pass
    finally:
        obs.stop()
        obs.join()


# ----------------------------------------------------------------------------- main
def main():
    setup_logging()
    args = set(sys.argv[1:])
    try:
        cfg = load_config()
    except RuntimeError as ex:
        log(f"Config error: {ex}")
        sys.exit(1)

    if "--watch" in args:
        watch(cfg)
    elif "--art" in args:
        run_steamgrid(cfg)
    elif "--status" in args:
        status(cfg)
    else:
        reconcile(cfg, dry=("--dry-run" in args))


if __name__ == "__main__":
    main()
