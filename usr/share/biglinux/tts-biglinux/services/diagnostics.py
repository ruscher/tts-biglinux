"""Diagnostics collector — gathers environment/backend info for support.

Pure data gathering (no UI): returns a structured dict and a human-readable
text block suitable for a "Copy diagnostic" button.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import platform
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Backends detected by the presence of their CLI/binary.
_BACKEND_BINARIES = {
    "espeak-ng": "espeak-ng",
    "piper-tts": "piper-tts",
    "koko (Kokoro)": "koko",
    "RHVoice": "RHVoice-test",
    "speech-dispatcher": "spd-say",
    "sox": "sox",
    "aplay (ALSA)": "aplay",
    "pw-play (PipeWire)": "pw-play",
}


def _native_engine_version() -> str:
    try:
        import tts_engine

        return tts_engine.version()
    except Exception:
        return "unavailable"


def _onnxruntime_version() -> str:
    """Resolve the system libonnxruntime version from its .so symlink target."""
    for base in ("/usr/lib", "/usr/lib64"):
        so = Path(base) / "libonnxruntime.so.1"
        try:
            if so.exists():
                target = os.path.realpath(so)
                # e.g. libonnxruntime.so.1.29.0 → 1.29.0
                name = Path(target).name
                ver = name.split(".so.", 1)[-1] if ".so." in name else ""
                return ver or target
        except OSError:
            continue
    return "not found"


def _count_piper_voices() -> int:
    count = 0
    for base in (
        "/usr/share/piper-voices",
        "/usr/local/share/piper-voices",
        str(Path.home() / ".local/share/piper-voices"),
    ):
        p = Path(base)
        if p.is_dir():
            count += sum(1 for _ in p.rglob("*.onnx"))
    return count


def collect_diagnostics(settings=None) -> dict:
    """Collect a structured diagnostics snapshot."""
    from config import APP_VERSION

    backends = {
        label: (shutil.which(binary) or "") for label, binary in _BACKEND_BINARIES.items()
    }

    active_backend = ""
    active_voice = ""
    if settings is not None:
        speech = getattr(settings, "speech", None)
        if speech is not None:
            active_backend = getattr(speech, "backend", "")
            active_voice = getattr(speech, "voice_id", "")

    return {
        "app_version": APP_VERSION,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "native_engine": _native_engine_version(),
        "onnxruntime": _onnxruntime_version(),
        "backends": backends,
        "piper_voices_installed": _count_piper_voices(),
        "active_backend": active_backend,
        "active_voice": active_voice,
        "session_type": os.environ.get("XDG_SESSION_TYPE", ""),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
    }


def _bus_name_owner(name: str) -> bool | None:
    """Someone owns ``name`` on the session bus (None: no bus)."""
    try:
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", GLib.Variant("(s)", (name,)), None, Gio.DBusCallFlags.NONE, 1000, None,
        )
        return bool(reply.unpack()[0])
    except Exception:
        return None


def _sni_host() -> bool | None:
    """A StatusNotifierItem host is registered (big-wm-tray, Plasma, snixembed…)."""
    if not _bus_name_owner("org.kde.StatusNotifierWatcher"):
        return False if _bus_name_owner("org.freedesktop.DBus") is not None else None
    try:
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            "org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
            "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", ("org.kde.StatusNotifierWatcher", "IsStatusNotifierHostRegistered")),
            None, Gio.DBusCallFlags.NONE, 1000, None,
        )
        return bool(reply.unpack()[0])
    except Exception:
        return None


def collect_integration(settings=None, app=None) -> dict:
    """How the shortcut → capture → speech → tray chain stands right now.

    Facts only, read without side effects: no file is changed, no
    compositor is signalled, a lazy XWayland is not started, and no
    selected or copied text is read.
    """
    from services import bigwm_shortcuts as bw
    from services import session_info, shortcut_service
    from services.desktop_integration_service import DesktopIntegrationService

    info = session_info.detect()
    env = {k: os.environ.get(k, "") for k in (
        "XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION",
        "XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY")}
    problems: list[str] = []

    accel = settings.shortcut.keybinding if settings is not None else ""
    command = DesktopIntegrationService._exec_path_for_speak()
    shortcut: dict = {"accel": accel, "keys": shortcut_service.display_text(accel) if accel and _gtk_ok() else accel,
                      "command": command, "command_exists": os.access(command, os.X_OK)}
    if not shortcut["command_exists"]:
        problems.append(f"the shortcut command {command} is missing")

    if info.is_big_windowmaker and accel:
        wl_path = bw.wlmaker_config_path(info.compositor_pid if info.compositor == "wlmaker" else None)
        try:
            ours, _all = bw.wlmaker_status(wl_path)
            shortcut["wlmaker"] = {"file": str(wl_path), "bound": ours, "expected": bw.to_wlmaker_key(accel)}
        except Exception as e:
            shortcut["wlmaker"] = {"file": str(wl_path), "error": str(e)}
        if info.compositor == "wlmaker" and info.compositor_pid:
            shortcut["wlmaker_reload"] = bw.wlmaker_can_reload(info.compositor_pid)
            headroom = bw.fd_headroom(info.compositor_pid)
            shortcut["wlmaker_fd_headroom"] = headroom
            if headroom is not None and headroom < bw.MIN_FD_HEADROOM:
                problems.append(f"wlmaker is about to run out of file descriptors ({headroom} left): "
                                "log in again (a wlmaker without the CLOEXEC fix leaks pipes)")
        wm_path = bw.wmaker_menu_path()
        try:
            menu = bw._wm_menu(wm_path.read_text(encoding="utf-8"))
            keys = [str(sc.value) for item, _ in bw._wm_walk(menu) if bw._wm_is_ours(item)
                    for sc in [bw._wm_item_command(item)[1]] if sc is not None]
            shortcut["windowmaker"] = {"file": str(wm_path), "bound": keys, "expected": bw.to_wmaker_key(accel)}
        except Exception as e:
            shortcut["windowmaker"] = {"file": str(wm_path), "error": str(e)}
        current = shortcut.get("wlmaker" if info.desktop == "wlmaker" else "windowmaker", {})
        if current.get("bound") is not None and current.get("expected") not in current.get("bound", []):
            problems.append(f"{info.desktop}: {current.get('expected')} is not in {current.get('file')}")
    elif info.desktop == "kde" and accel:
        active = shortcut_service.kde_active_keys()
        shortcut["kglobalaccel"] = active if active is not None else "unreachable"
    if app is not None:
        st = app.shortcut_status
        shortcut["status"] = {"registered": st.registered, "pending": st.pending,
                              "message": st.message, "notes": st.notes}
        shortcut["last_used"] = app.last_speak_request

    capture: dict = {"tools": {t: bool(shutil.which(t)) for t in ("wl-paste", "xsel", "xclip")},
                     "clipboard_fallback": bool(settings.shortcut.clipboard_fallback) if settings is not None else False}
    if info.display_server == "wayland":
        from services.wayland_globals import selection_support

        capture["wayland"] = selection_support()
        if capture["wayland"].get("primary") is False:
            problems.append("the compositor offers no primary selection (zwp_primary_selection_device_manager_v1)")
        if not capture["tools"]["wl-paste"]:
            problems.append("wl-paste (wl-clipboard) is not installed")
        capture["xwayland_running"] = info.xwayland
        if info.xwayland and os.environ.get("DISPLAY"):
            from services import x11_selection

            f = x11_selection.focus(os.environ["DISPLAY"])
            if f is not None:
                capture["xwayland_focus"] = {"x11_app_focused": f.active_is_x11_client,
                                             "x11_primary_owned_by_app": f.primary_is_x11_client}
        if info.compositor == "wlmaker":
            capture["xwayland_selection_bridge"] = _wlmaker_bridges_selection(info.compositor_pid)
    elif info.display_server == "x11":
        if not (capture["tools"]["xsel"] or capture["tools"]["xclip"]):
            problems.append("neither xsel nor xclip is installed")
    if app is not None:
        capture["last"] = app.last_capture

    tray: dict = {"sni_host": _sni_host(),
                  "pyside6": bool(importlib.util.find_spec("PySide6")),
                  "notifications": _bus_name_owner("org.freedesktop.Notifications")}
    if settings is not None:
        tray["enabled"] = settings.shortcut.show_in_launcher
    if app is not None:
        tray["state"] = app.tray_state
        tray["detail"] = app.tray_detail
    if tray["enabled"] if "enabled" in tray else True:
        if tray["sni_host"] is False:
            problems.append("no system tray host (StatusNotifierWatcher); the app works without the icon")
        if not tray["pyside6"]:
            problems.append("PySide6 is not installed: no tray icon")

    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    audio = {"pipewire": bool(runtime) and os.path.exists(os.path.join(runtime, "pipewire-0")),
             "pulse": bool(runtime) and os.path.exists(os.path.join(runtime, "pulse", "native")),
             "aplay": bool(shutil.which("aplay")), "pw-play": bool(shutil.which("pw-play"))}
    if not audio["aplay"]:
        problems.append("aplay (alsa-utils) is missing: no engine can play")

    return {
        "session": {"desktop": info.desktop, "display_server": info.display_server,
                    "compositor": info.compositor, "compositor_pid": info.compositor_pid,
                    "evidence": list(info.evidence), "env": env},
        "app_running": _bus_name_owner("br.com.biglinux.tts"),
        "shortcut": shortcut,
        "capture": capture,
        "tray": tray,
        "audio": audio,
        "problems": problems,
    }


def _gtk_ok() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk  # noqa: F401

        return True
    except (ImportError, ValueError):
        return False


def _wlmaker_bridges_selection(pid: int | None) -> bool | None:
    """Does this wlmaker give XWayland a seat (selection bridge X11↔Wayland)?

    Without wlr_xwayland_set_seat wlroots copies no selection between the
    two; the capture then reads XWayland's PRIMARY directly.
    """
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/exe", "rb") as f:
            return b"wlr_xwayland_set_seat" in f.read()
    except OSError:
        return None


def format_integration(data: dict) -> str:
    s, sc, cap, tray, audio = data["session"], data["shortcut"], data["capture"], data["tray"], data["audio"]

    def yn(v) -> str:
        return {True: "yes", False: "no", None: "unknown"}.get(v, str(v))

    lines = [
        "Desktop integration",
        f"  Desktop:          {s['desktop']}  (display server: {s['display_server']}"
        + (f", compositor: {s['compositor']}[{s['compositor_pid']}]" if s["compositor"] else "") + ")",
        f"  Detected from:    {', '.join(s['evidence']) or '-'}",
        "  Session vars:     " + "  ".join(f"{k}={v or '-'}" for k, v in s["env"].items()),
        f"  App running:      {yn(data['app_running'])}",
        f"  Shortcut:         {sc.get('keys') or '-'} → {sc['command']}{'' if sc['command_exists'] else '  (MISSING)'}",
    ]
    for name in ("wlmaker", "windowmaker"):
        if name in sc:
            d = sc[name]
            state = d.get("error") or (f"bound: {', '.join(d['bound']) or 'none'} (expected {d['expected']})")
            lines.append(f"    {name:<16}{state}  [{d['file']}]")
    if "wlmaker_reload" in sc:
        lines.append(f"    wlmaker reload: {'SIGHUP (applies at once)' if sc['wlmaker_reload'] else 'not supported: next login'}")
    if sc.get("wlmaker_fd_headroom") is not None:
        lines.append(f"    wlmaker fds:    {sc['wlmaker_fd_headroom']} free")
    if "kglobalaccel" in sc:
        lines.append(f"    KGlobalAccel:   {sc['kglobalaccel']}")
    if "status" in sc:
        st = sc["status"]
        lines.append(f"    status:         registered={yn(st['registered'])} pending={yn(st['pending'])} {st['message']}")
        for note in st.get("notes") or []:
            lines.append(f"    also:           {note}")
    if sc.get("last_used"):
        import time

        lines.append(f"    last used:      {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(sc['last_used']))}")
    tools = ", ".join(f"{t} {'✔' if ok else '✘'}" for t, ok in cap["tools"].items())
    lines.append(f"  Capture tools:    {tools}; read copied text: {yn(cap['clipboard_fallback'])}")
    if "wayland" in cap:
        w = cap["wayland"]
        lines.append(f"  Wayland:          primary selection {yn(w['primary'])}, data-control {w['data_control'] or 'no'}")
        lines.append(f"  XWayland:         running {yn(cap.get('xwayland_running'))}"
                     + (f", X11↔Wayland selection bridge {yn(cap['xwayland_selection_bridge'])}" if "xwayland_selection_bridge" in cap else ""))
        if "xwayland_focus" in cap:
            f = cap["xwayland_focus"]
            lines.append(f"                    X11 app focused {yn(f['x11_app_focused'])}, X11 PRIMARY held by an app {yn(f['x11_primary_owned_by_app'])}")
    if cap.get("last"):
        lines.append(f"  Last capture:     {cap['last']}")
    lines.append(f"  Tray:             host (SNI) {yn(tray['sni_host'])}, PySide6 {yn(tray['pyside6'])}"
                 + (f", enabled {yn(tray['enabled'])}" if "enabled" in tray else "")
                 + (f", icon {tray['state']}" if "state" in tray else "")
                 + (f" ({tray['detail']})" if tray.get("detail") else ""))
    lines.append(f"  Notifications:    {yn(tray['notifications'])}")
    lines.append(f"  Audio:            PipeWire {yn(audio['pipewire'])}, PulseAudio socket {yn(audio['pulse'])}, "
                 f"aplay {yn(audio['aplay'])}, pw-play {yn(audio['pw-play'])}")
    lines.append("  Problems:         " + ("none found" if not data["problems"] else ""))
    for p in data["problems"]:
        lines.append(f"    - {p}")
    return "\n".join(lines)


def format_diagnostics(data: dict) -> str:
    """Render the diagnostics dict as a copy-pastable text block."""
    lines = [
        "BigLinux TTS — Diagnostic",
        f"App version:      {data.get('app_version')}",
        f"Python:           {data.get('python_version')}",
        f"Platform:         {data.get('platform')}",
        f"Session/Desktop:  {data.get('session_type')} / {data.get('desktop')}",
        f"Native engine:    {data.get('native_engine')}",
        f"ONNX Runtime:     {data.get('onnxruntime')}",
        f"Piper voices:     {data.get('piper_voices_installed')}",
        f"Active backend:   {data.get('active_backend') or '(none)'}",
        f"Active voice:     {data.get('active_voice') or '(none)'}",
        "Backends detected:",
    ]
    for label, path in data.get("backends", {}).items():
        lines.append(f"  - {label:22} {'✔ ' + path if path else '✘ not found'}")
    return "\n".join(lines)
