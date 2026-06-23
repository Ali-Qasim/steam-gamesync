#!/usr/bin/env python3
"""
Remove the gamesync background watcher.

  python uninstall.py

Stops the running watcher, removes the logon scheduled task, and optionally
restores the most recent shortcuts.vdf backup. Does NOT delete your files,
.env, or downloaded artwork.
"""

import os
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
TASK_NAME = "GameSync"


def ask_yes(prompt, default=True):
    d = "Y/n" if default else "y/N"
    val = input(f"{prompt} ({d}): ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")


def stop_watcher():
    # Find any python/pythonw process running gamesync.py --watch and kill it.
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*gamesync.py*--watch*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; \"Stopped PID \" + $_.ProcessId }"
    )
    res = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True)
    out = (res.stdout or "").strip()
    print(out or "No running watcher found.")


def remove_task():
    res = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                         capture_output=True, text=True)
    print((res.stdout or res.stderr or "").strip())


def restore_backup():
    try:
        sys.path.insert(0, HERE)
        import gamesync
        cfg = gamesync.load_config()
        uid = gamesync.resolve_userdata(cfg)
        sc = gamesync.shortcuts_path(cfg, uid)
    except Exception as ex:
        print(f"Could not locate shortcuts.vdf: {ex}")
        return
    bak = sc + ".gamesync.bak"
    if not os.path.isfile(bak):
        print("No backup found.")
        return
    if ask_yes(f"Restore {bak}\n     -> {sc}?", default=False):
        import shutil
        shutil.copy2(bak, sc)
        print("Restored. Restart Steam to load it.")


def main():
    print("Removing gamesync watcher...")
    stop_watcher()
    remove_task()
    restore_backup()
    print("\nDone. Your files and .env are untouched; delete the folder to fully remove.")


if __name__ == "__main__":
    main()
