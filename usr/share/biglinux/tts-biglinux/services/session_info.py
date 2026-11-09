"""
Which desktop, which display server, which compositor — kept apart.

* desktop:        the environment that owns global shortcuts (kde, gnome,
                  xfce, cinnamon, wlmaker, windowmaker, unknown)
* display_server: wayland | x11 | unknown — from the sockets, not from
                  ``DISPLAY`` alone (XWayland gives every Wayland session one)
* compositor:     the process really serving the session, when it can be
                  found: on Wayland, whoever holds ``$WAYLAND_DISPLAY.lock``
                  (the way big-wm-wprefs-sync finds wlmaker); on X11, a
                  running window manager we integrate with (``wmaker``)

The environment variables give the first answer; a running compositor
confirms or corrects it (a nested session, a variable left over from another
session). ``evidence`` lists what was used, for diagnostics.

Pure /proc and environment reads: no X or Wayland connection is opened here,
so a lazily started XWayland is never woken up by a check.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Desktop ids → names they go by in XDG_CURRENT_DESKTOP / XDG_SESSION_DESKTOP
# / DESKTOP_SESSION (compared lowercase, token by token).
_DESKTOP_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Big WindowMaker first: its session names contain no other desktop's.
    ("wlmaker", ("wlmaker", "waylandmaker", "wayland-maker", "big-wm-wayland")),
    ("windowmaker", ("windowmaker", "window-maker", "wmaker", "big-wm", "gnustep")),
    ("kde", ("kde", "plasma", "plasmawayland", "plasmax11")),
    ("gnome", ("gnome", "gnome-xorg", "gnome-wayland", "unity", "ubuntu")),
    ("xfce", ("xfce", "xfce4", "xubuntu")),
    ("cinnamon", ("cinnamon", "x-cinnamon", "cinnamon2d")),
)

# Compositor / window manager process name (comm) → desktop id.
_COMPOSITOR_DESKTOP = {
    "wlmaker": "wlmaker",
    "wmaker": "windowmaker",
    "kwin_wayland": "kde",
    "kwin_x11": "kde",
    "gnome-shell": "gnome",
    "cinnamon": "cinnamon",
    "xfwm4": "xfce",
}

BIG_WINDOWMAKER = ("wlmaker", "windowmaker")


@dataclass(frozen=True)
class SessionInfo:
    desktop: str = "unknown"
    display_server: str = "unknown"
    compositor: str = ""  # process name, "" when not found
    compositor_pid: int | None = None
    xwayland: bool = False  # an XWayland server already runs (Wayland only)
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_big_windowmaker(self) -> bool:
        return self.desktop in BIG_WINDOWMAKER


# ── environment ──────────────────────────────────────────────────────


def _tokens(env: dict) -> list[tuple[str, str]]:
    """(variable, token) pairs, in order of trust."""
    out = []
    for var in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION"):
        raw = env.get(var, "") or ""
        if var == "DESKTOP_SESSION":
            raw = Path(raw).name  # some display managers store a path
        for tok in raw.replace(";", ":").split(":"):
            tok = tok.strip().lower()
            if tok:
                out.append((var, tok))
    return out


def desktop_from_env(env: dict | None = None) -> tuple[str, str]:
    """Desktop id named by the session variables, and which one said it."""
    env = os.environ if env is None else env
    for var, tok in _tokens(env):
        for desktop, names in _DESKTOP_TOKENS:
            if tok in names:
                return desktop, f"{var}={tok}"
    # Substring matches last ("plasma-something", "x-cinnamon-…").
    for var, tok in _tokens(env):
        for desktop, names in _DESKTOP_TOKENS:
            if any(n in tok for n in names if len(n) > 3):
                return desktop, f"{var}~{tok}"
    return "unknown", ""


def wayland_socket(env: dict | None = None) -> Path | None:
    env = os.environ if env is None else env
    name = env.get("WAYLAND_DISPLAY")
    if not name:
        return None
    if os.path.isabs(name):
        return Path(name)
    runtime = env.get("XDG_RUNTIME_DIR")
    return Path(runtime) / name if runtime else None


def display_server_from_env(env: dict | None = None) -> tuple[str, str]:
    """wayland | x11 | unknown, and why.

    A Wayland socket that exists wins: XWayland sets DISPLAY in every
    Wayland session, so DISPLAY alone does not mean X11.
    """
    env = os.environ if env is None else env
    sock = wayland_socket(env)
    if sock is not None and sock.exists():
        return "wayland", f"socket {sock.name}"
    stype = (env.get("XDG_SESSION_TYPE") or "").lower()
    if stype == "wayland" and env.get("WAYLAND_DISPLAY"):
        return "wayland", "XDG_SESSION_TYPE=wayland"
    if env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        return "x11", f"DISPLAY={env.get('DISPLAY')}"
    if stype == "x11":
        return "x11", "XDG_SESSION_TYPE=x11"
    return "unknown", ""


# ── processes ────────────────────────────────────────────────────────

_PROC = Path("/proc")


def _own_processes(names: set[str]) -> dict[str, list[int]]:
    """PIDs of this user's processes whose comm is in ``names``."""
    found: dict[str, list[int]] = {}
    uid = os.getuid()
    try:
        entries = os.listdir(_PROC)
    except OSError:
        return found
    for d in entries:
        if not d.isdigit():
            continue
        try:
            comm = (_PROC / d / "comm").read_text().strip()
            if comm in names and os.stat(_PROC / d).st_uid == uid:
                found.setdefault(comm, []).append(int(d))
        except OSError:
            continue
    return found


def process_name(pid: int) -> str:
    try:
        return (_PROC / str(pid) / "comm").read_text().strip()
    except OSError:
        return ""


def wayland_compositor_pid(env: dict | None = None) -> int | None:
    """PID holding the flock on ``$WAYLAND_DISPLAY.lock`` (the compositor)."""
    sock = wayland_socket(env)
    if sock is None:
        return None
    try:
        inode = os.stat(str(sock) + ".lock").st_ino
        locks = (_PROC / "locks").read_text().splitlines()
    except OSError:
        return None
    for line in locks:
        p = line.split()
        if len(p) >= 6 and p[1] == "FLOCK" and p[4].isdigit():
            dev_inode = p[5].split(":")
            if len(dev_inode) == 3 and dev_inode[2].isdigit() and int(dev_inode[2]) == inode:
                return int(p[4])
    return None


def xwayland_running(env: dict | None = None) -> bool:
    """An Xwayland server for this session's DISPLAY is already up.

    With wlmaker's --lazy_xwayland the DISPLAY socket exists from login but
    the server starts on the first X11 client: connecting to check would
    start it (~35 MB). So look for the process instead.
    """
    env = os.environ if env is None else env
    display = env.get("DISPLAY", "")
    pids = _own_processes({"Xwayland"}).get("Xwayland", [])
    if not pids:
        return False
    if not display:
        return True
    for pid in pids:
        try:
            argv = (_PROC / str(pid) / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if display.encode() in argv:
            return True
    return False


# ── detection ────────────────────────────────────────────────────────


def detect(env: dict | None = None) -> SessionInfo:
    env = os.environ if env is None else env
    evidence = []
    desktop, why = desktop_from_env(env)
    if why:
        evidence.append(why)
    server, why = display_server_from_env(env)
    if why:
        evidence.append(why)

    compositor, pid = "", None
    if server == "wayland":
        pid = wayland_compositor_pid(env)
        if pid:
            compositor = process_name(pid)
            evidence.append(f"compositor {compositor}[{pid}]")
    elif server == "x11":
        wm = _own_processes({"wmaker"}).get("wmaker", [])
        if wm:
            compositor, pid = "wmaker", wm[0]
            evidence.append(f"window manager wmaker[{pid}]")

    # A running compositor we know overrides a missing or stale variable.
    by_process = _COMPOSITOR_DESKTOP.get(compositor)
    if by_process and by_process != desktop:
        evidence.append(f"desktop {desktop} → {by_process} (from the compositor)")
        desktop = by_process

    xw = server == "wayland" and xwayland_running(env)
    return SessionInfo(desktop, server, compositor, pid, xw, tuple(evidence))


__all__ = [
    "BIG_WINDOWMAKER",
    "SessionInfo",
    "desktop_from_env",
    "detect",
    "display_server_from_env",
    "process_name",
    "wayland_compositor_pid",
    "xwayland_running",
]
