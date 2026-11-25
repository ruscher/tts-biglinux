#!/usr/bin/env python3
"""End-to-end check of the global shortcut in a real Wayland session.

Selected text → the shortcut pressed through the compositor → the
compositor runs biglinux-tts-speak → the app captures the selection, speaks
and shows its tray icon → the shortcut again stops it.

Everything is observed from outside, the way a user would notice it:

* the app's D-Bus name (br.com.biglinux.tts) appears,
* its MPRIS player (org.mpris.MediaPlayer2.biglinux-tts) reports Playing,
  then Stopped after the second press,
* the tray host lists its icon (big-wm-tray --state, when available),
* timings: key press → name on the bus → Playing, and second press → Stopped.

Audio is really played (one short sentence). Needs: a compositor with
zwp_virtual_keyboard_manager_v1 (wlmaker), the shortcut registered
(biglinux-tts --repair-shortcut), and wl-copy. Text sources:

    --source wayland   wl-copy --primary (a Wayland client owns the selection)
    --source gtk       a GTK 4 window with the text selected in an entry
    --source xwayland  an xterm (XWayland) owning PRIMARY, focused

    python3 tests/integration/shortcut_e2e.py --combo alt+n --source gtk
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEXT = "Leitura de teste: ação, coração e 2 números."


def bus(method: str, *args: str) -> str:
    return subprocess.run(["gdbus", "call", "--session", *args, "--method", method],
                          capture_output=True, text=True).stdout.strip()


def has_name(name: str) -> bool:
    return "true" in bus("org.freedesktop.DBus.NameHasOwner", "--dest", "org.freedesktop.DBus",
                         "--object-path", "/org/freedesktop/DBus", name)


def playback() -> str:
    out = bus("org.freedesktop.DBus.Properties.Get", "--dest", "org.mpris.MediaPlayer2.biglinux-tts",
              "--object-path", "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", "PlaybackStatus")
    return out.split("'")[1] if "'" in out else ""


def wait(cond, timeout: float) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return time.monotonic() - t0
        time.sleep(0.02)
    return None


def press(combo: str) -> None:
    subprocess.run([sys.executable, str(HERE / "wl_press_keys.py"), combo], check=True)


GTK_SOURCE = r'''
import sys, gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib
app = Gtk.Application(application_id="br.com.biglinux.tts.e2e")
def on_activate(app):
    win = Gtk.ApplicationWindow(application=app, title="tts-e2e-gtk")
    entry = Gtk.Entry(text=sys.argv[1])
    win.set_child(entry); win.present()
    def select():
        entry.grab_focus(); entry.select_region(0, -1); print("selected", flush=True); return False
    GLib.timeout_add(400, select)
    GLib.timeout_add_seconds(int(sys.argv[2]), app.quit)
app.connect("activate", on_activate)
app.run([sys.argv[0]])
'''


def start_source(kind: str, seconds: int):
    if kind == "wayland":
        subprocess.run(["wl-copy", "--primary"], input=TEXT.encode(), check=True)
        return None
    if kind == "gtk":
        p = subprocess.Popen([sys.executable, "-c", GTK_SOURCE, TEXT, str(seconds)], stdout=subprocess.PIPE, text=True)
        p.stdout.readline()
        time.sleep(0.3)
        return p
    if kind == "xwayland":
        b64 = base64.b64encode(TEXT.encode()).decode()
        p = subprocess.Popen(["xterm", "-xrm", "XTerm*allowWindowOps: true", "-xrm", "XTerm*disallowedWindowOps: ",
                              "-title", "tts-e2e-xterm", "-e", "bash", "-c", f"printf '\\e]52;p;{b64}\\a'; sleep {seconds}"])
        time.sleep(0.8)
        return p
    raise SystemExit(f"unknown source {kind}")


def tray_listed() -> bool | None:
    if not shutil.which("big-wm-tray"):
        return None
    out = subprocess.run(["big-wm-tray", "--state"], capture_output=True, text=True).stdout
    return "BigLinux TTS" in out or "biglinux" in out.lower() or "tts" in out.lower()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--combo", default="alt+v")
    ap.add_argument("--source", default="wayland", choices=("wayland", "gtk", "xwayland"))
    ap.add_argument("--json")
    ap.add_argument("--log", help="the app's --debug log (see --own-instance): "
                    "the capture's source and length are checked in it")
    a = ap.parse_args()

    result: dict = {"combo": a.combo, "source": a.source, "cold": not has_name("br.com.biglinux.tts")}
    mark = Path(a.log).stat().st_size if a.log else 0
    src = start_source(a.source, 25)
    try:
        t0 = time.monotonic()
        press(a.combo)
        result["app_on_bus_s"] = wait(lambda: has_name("br.com.biglinux.tts"), 15)
        result["playing_s"] = wait(lambda: playback() == "Playing", 20)
        if result["playing_s"] is not None:
            result["playing_s"] = round(time.monotonic() - t0, 3)
        result["tray_listed"] = wait(lambda: tray_listed() is not False, 8) is not None if shutil.which("big-wm-tray") else None
        if result["playing_s"] is not None:
            press(a.combo)
            stopped = wait(lambda: playback() != "Playing", 10)
            result["stop_s"] = round(stopped, 3) if stopped is not None else None
    finally:
        if src is not None:
            src.terminate()
    ok = result["playing_s"] is not None and result.get("stop_s") is not None
    if a.log:
        with open(a.log, encoding="utf-8", errors="replace") as f:
            f.seek(mark)
            caps = [ln.split("captured ", 1)[1].strip() for ln in f if "Tray speak: captured" in ln]
        expected = f"{len(TEXT)} chars from " + ("xwayland-primary" if a.source == "xwayland" else "wayland-primary")
        result["captured"] = caps[0] if caps else None
        ok = ok and result["captured"] == expected
    result["ok"] = ok
    print(json.dumps(result, indent=2))
    if a.json:
        Path(a.json).write_text(json.dumps(result, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
