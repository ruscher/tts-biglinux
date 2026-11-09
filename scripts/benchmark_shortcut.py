#!/usr/bin/env python3
"""Measure the global-shortcut path in a real graphical session.

Selects a short text (Wayland: wl-copy --primary; X11: xsel -p -i), then runs
the command the shortcut runs (usr/bin/biglinux-tts-speak of this checkout)
the way the compositor does, and times every step from the app's debug log:

  cold     no instance running: command → text captured → audio starts
  warm     instance running:    command → text captured → audio starts
  stop     command while reading → reading stopped
  forward  how long the shortcut's own process lives (client + hand-over)

It also reports the app's memory (RSS/PSS, tray helper included), the number
of processes and the CPU used while idle. Audio is really played (one short
word per run). Nothing in the user's settings is changed.

    python3 scripts/benchmark_shortcut.py [--runs 5] [--json out.json]

Stop any running BigLinux TTS first: the cold run needs to start it.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEAK = REPO / "usr" / "bin" / "biglinux-tts-speak"
TEXT = "Teste."


def select_text(text: str) -> None:
    """Make ``text`` the current selection, like a mouse selection does."""
    if os.environ.get("WAYLAND_DISPLAY"):
        subprocess.run(["wl-copy", "--primary"], input=text.encode(), check=True)
    else:
        subprocess.Popen(["xsel", "-p", "-i"], stdin=subprocess.PIPE).communicate(text.encode())


def app_pids() -> list[int]:
    """The app's main process and its children (tray helper, players)."""
    out = subprocess.run(["pgrep", "-f", "tts-biglinux/main.py|biglinux-tts"], capture_output=True, text=True).stdout
    mains = [int(p) for p in out.split() if int(p) != os.getpid()]
    pids = set(mains)
    for pid in mains:
        kids = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout
        pids.update(int(k) for k in kids.split())
    return sorted(pids)


def mem_kib(pid: int) -> tuple[int, int]:
    rss = pss = 0
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            if line.startswith("Rss:"):
                rss = int(line.split()[1])
            elif line.startswith("Pss:"):
                pss = int(line.split()[1])
    except OSError:
        pass
    return rss, pss


def cpu_ticks(pids: list[int]) -> int:
    total = 0
    for pid in pids:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            total += int(fields[11]) + int(fields[12])  # utime + stime
        except (OSError, IndexError):
            pass
    return total


class AppLog:
    """Lines of the primary instance's debug log, stamped on arrival."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, args=(proc,), daemon=True).start()

    def _pump(self, proc) -> None:
        for line in proc.stderr:
            self.q.put((time.monotonic(), line.rstrip()))

    def wait_for(self, needle: str, timeout: float = 60.0) -> float | None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                ts, line = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if needle in line:
                return ts
        return None

    def drain(self) -> None:
        while not self.q.empty():
            self.q.get_nowait()


def wait_idle(log: AppLog, timeout: float = 30.0) -> None:
    log.wait_for("→ TTSState.IDLE", timeout)
    time.sleep(1.0)
    log.drain()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--json", help="write the results here")
    args = parser.parse_args()

    if app_pids():
        print("BigLinux TTS is running: quit it first (the cold run starts it).", file=sys.stderr)
        return 1
    for tool in ("wl-copy",) if os.environ.get("WAYLAND_DISPLAY") else ("xsel",):
        if not shutil.which(tool):
            print(f"{tool} is needed", file=sys.stderr)
            return 1

    results: dict = {"session": {k: os.environ.get(k, "") for k in (
        "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "WAYLAND_DISPLAY", "DISPLAY")}}

    # ── cold start ──
    select_text(TEXT)
    t0 = time.monotonic()
    primary = subprocess.Popen([str(SPEAK), "--debug"], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    log = AppLog(primary)
    t_cap = log.wait_for("captured")
    t_audio = log.wait_for("→ TTSState.SPEAKING")
    results["cold"] = {
        "capture_ms": round((t_cap - t0) * 1000) if t_cap else None,
        "audio_ms": round((t_audio - t0) * 1000) if t_audio else None,
    }
    wait_idle(log)

    # ── warm runs ──
    warm_cap, warm_audio, forward, stop = [], [], [], []
    for _ in range(args.runs):
        select_text(TEXT)
        t0 = time.monotonic()
        client = subprocess.run([str(SPEAK)], capture_output=True)
        forward.append((time.monotonic() - t0) * 1000)
        t_cap = log.wait_for("captured")
        t_audio = log.wait_for("→ TTSState.SPEAKING")
        if t_cap:
            warm_cap.append((t_cap - t0) * 1000)
        if t_audio:
            warm_audio.append((t_audio - t0) * 1000)
            # Press again while reading: the default mode stops.
            t1 = time.monotonic()
            subprocess.run([str(SPEAK)], capture_output=True)
            t_stop = log.wait_for("→ TTSState.IDLE", 10)
            if t_stop:
                stop.append((t_stop - t1) * 1000)
        if client.returncode:
            print("client exit", client.returncode, client.stderr.decode()[-300:], file=sys.stderr)
        wait_idle(log)

    def summary(values):
        return {"median_ms": round(statistics.median(values)), "min_ms": round(min(values)),
                "max_ms": round(max(values)), "n": len(values)} if values else None

    results["warm_capture"] = summary(warm_cap)
    results["warm_audio"] = summary(warm_audio)
    results["forward"] = summary(forward)
    results["stop"] = summary(stop)

    # ── idle footprint ──
    time.sleep(2)
    pids = app_pids()
    mem = {pid: mem_kib(pid) for pid in pids}
    c0 = cpu_ticks(pids)
    time.sleep(10)
    c1 = cpu_ticks(pids)
    hz = os.sysconf("SC_CLK_TCK")
    results["idle"] = {
        "processes": len(pids),
        "rss_mib": round(sum(r for r, _ in mem.values()) / 1024, 1),
        "pss_mib": round(sum(p for _, p in mem.values()) / 1024, 1),
        "cpu_percent_10s": round((c1 - c0) / hz / 10 * 100, 2),
    }

    primary.terminate()
    try:
        primary.wait(5)
    except subprocess.TimeoutExpired:
        primary.kill()
    time.sleep(1)
    results["leftover_processes"] = len(app_pids())

    print(json.dumps(results, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
