"""Big WindowMaker integration: session detection, the shortcut in wlmaker's
and Window Maker's own files, and selection capture on Wayland/XWayland/X11.

Every file lives in a scratch folder (conftest points XDG_CONFIG_HOME and
GNUSTEP_USER_ROOT there) and no compositor is ever signalled.
"""

from __future__ import annotations

import os
import socket
import struct
import threading
from pathlib import Path

import pytest

from services import bigwm_shortcuts as bw
from services import clipboard_service as cs
from services import plist, session_info, shortcut_service, wayland_globals
from services.x11_selection import X11Focus

CMD = "/usr/bin/biglinux-tts-speak"

CONFIG = """// Configuração do wlmaker (comentário do usuário).
{
    Keyboard = {
        XkbRMLVO = { Rules = "evdev"; Model = "abnt2"; Layout = "br"; };
    };
    // Mesmos atalhos da sessão X11.
    KeyBindings = {
        "Logo+V" = (ShellExecute, "big-wm-open clipboard");
        "Alt+Tab" = TaskNext;
        "Ctrl+Alt+T" = (ShellExecute, "big-wm-open terminal");
    };
    Autostart = ("/usr/bin/big-wm-session-ctl start");
}
"""

MENU = """(
  "Big WindowMaker",
  (Aplicativos, OPEN_PLMENU, "|| big-wm-appmenu"),
  (Terminal, SHORTCUT, "Control+Mod1+t", EXEC, "big-wm-open terminal"),
  ("Área de transferência", SHORTCUT, "Mod4+v", EXEC, "big-wm-open clipboard"),
  (
    Sessão,
    ("Bloquear tela", SHORTCUT, "Mod4+l", EXEC, "big-wm-lock"),
    (Sair, EXIT)
  )
)
"""


@pytest.fixture
def files(_never_touch_the_desktop):
    root = _never_touch_the_desktop
    cfg = root / ".config" / "wlmaker" / "Config.plist"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(CONFIG, encoding="utf-8")
    menu = root / "GNUstep" / "Defaults" / "WMRootMenu"
    menu.parent.mkdir(parents=True)
    menu.write_text(MENU, encoding="utf-8")
    return cfg, menu


# ── session detection ────────────────────────────────────────────────


@pytest.mark.parametrize("env, desktop", [
    ({"XDG_CURRENT_DESKTOP": "wlmaker", "DESKTOP_SESSION": "big-wm-wayland"}, "wlmaker"),
    ({"DESKTOP_SESSION": "big-wm-wayland"}, "wlmaker"),
    ({"XDG_CURRENT_DESKTOP": "WindowMaker"}, "windowmaker"),
    ({"DESKTOP_SESSION": "/usr/share/xsessions/big-wm"}, "windowmaker"),
    ({"XDG_SESSION_DESKTOP": "wmaker"}, "windowmaker"),
    ({"XDG_CURRENT_DESKTOP": "KDE", "DESKTOP_SESSION": "plasma"}, "kde"),
    ({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, "gnome"),
    ({"XDG_CURRENT_DESKTOP": "XFCE"}, "xfce"),
    ({"XDG_CURRENT_DESKTOP": "X-Cinnamon"}, "cinnamon"),
    ({"XDG_CURRENT_DESKTOP": "Hyprland"}, "unknown"),
])
def test_desktop_from_the_session_variables(env, desktop):
    assert session_info.desktop_from_env(env)[0] == desktop


def test_display_alone_does_not_mean_x11(tmp_path):
    (tmp_path / "wayland-0").touch()
    env = {"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": str(tmp_path), "DISPLAY": ":0"}
    assert session_info.display_server_from_env(env)[0] == "wayland"
    assert session_info.display_server_from_env({"DISPLAY": ":0"})[0] == "x11"
    assert session_info.display_server_from_env({})[0] == "unknown"


def test_running_compositor_corrects_the_variables(monkeypatch, tmp_path):
    (tmp_path / "wayland-0").touch()
    monkeypatch.setattr(session_info, "wayland_compositor_pid", lambda env=None: 4242)
    monkeypatch.setattr(session_info, "process_name", lambda pid: "wlmaker")
    info = session_info.detect({"XDG_CURRENT_DESKTOP": "", "WAYLAND_DISPLAY": "wayland-0",
                                "XDG_RUNTIME_DIR": str(tmp_path)})
    assert info.desktop == "wlmaker" and info.compositor_pid == 4242 and info.is_big_windowmaker


def test_legacy_detection_knows_big_windowmaker(monkeypatch):
    from services.desktop_integration_service import DesktopIntegrationService as DIS

    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "wlmaker")
    assert DIS.detect_desktop_environment() == "wlmaker"
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    assert DIS.detect_desktop_environment() == "kde"


# ── keys ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("accel, wl, wm", [
    ("<Alt>v", "Alt+V", "Mod1+v"),
    ("<Control><Alt>t", "Ctrl+Alt+T", "Control+Mod1+t"),
    ("<Super>space", "Logo+space", "Mod4+space"),
    ("<Shift><Alt>F9", "Alt+Shift+F9", "Mod1+Shift+F9"),
    ("<Primary>n", "Ctrl+N", "Control+n"),
])
def test_key_names_for_each_window_manager(accel, wl, wm):
    assert bw.to_wlmaker_key(accel) == wl
    assert bw.to_wmaker_key(accel) == wm
    assert bw.wlmaker_combo(wl) == bw.wmaker_combo(wm) == bw.combo_of(accel)


# ── plist ────────────────────────────────────────────────────────────


def test_plist_keeps_positions_and_rejects_what_wlmaker_cannot_read():
    root = plist.check(CONFIG, "wlmaker")
    kb = root.entry("KeyBindings").value
    assert [e.key.value for e in kb.entries] == ["Logo+V", "Alt+Tab", "Ctrl+Alt+T"]
    first = kb.entries[0]
    assert CONFIG[first.start:first.end].startswith('"Logo+V" = (ShellExecute')
    for bad in ('{ a = /* c */ b; }', "{ a = <00ff>; }", "{ a = big-wm; }", "{ a = b", "{ a = (b, c; }"):
        with pytest.raises(plist.PlistError):
            plist.check(bad, "wlmaker")
    assert plist.check("{ a = big-wm; }", "wings").to_py() == {"a": "big-wm"}


# ── wlmaker KeyBindings ──────────────────────────────────────────────


def test_wlmaker_binding_added_once_and_removed_byte_for_byte(files):
    cfg, _ = files
    r = bw.set_wlmaker("<Alt>v", command=CMD)
    assert r.state == bw.PENDING and r.changed  # no compositor to reload in tests
    assert r.backup and r.backup.read_text(encoding="utf-8") == CONFIG
    text = cfg.read_text(encoding="utf-8")
    assert text.count(f'"Alt+V" = (ShellExecute, "{CMD}");') == 1
    assert "// Configuração do wlmaker (comentário do usuário)." in text
    assert bw.wlmaker_status(cfg)[0] == ["Alt+V"]

    again = bw.set_wlmaker("<Alt>v", command=CMD)
    assert not again.changed and cfg.read_text(encoding="utf-8") == text

    bw.set_wlmaker("<Alt>n", command=CMD)  # changing the key replaces it
    assert bw.wlmaker_status(cfg)[0] == ["Alt+N"]

    assert bw.set_wlmaker(None).state == bw.REMOVED
    assert cfg.read_text(encoding="utf-8") == CONFIG


def test_wlmaker_conflict_writes_nothing(files):
    cfg, _ = files
    r = bw.set_wlmaker("<Control><Alt>t", command=CMD)
    assert r.state == bw.CONFLICT and r.conflicts == ["Ctrl+Alt+T → ShellExecute big-wm-open terminal"]
    assert cfg.read_text(encoding="utf-8") == CONFIG
    assert bw.set_wlmaker("<Super>v", command=CMD).state == bw.CONFLICT  # case-insensitive


def test_wlmaker_invalid_file_is_never_touched(files):
    cfg, _ = files
    broken = CONFIG.replace("TaskNext;", "TaskNext")  # missing ';' before another entry
    cfg.write_text(broken, encoding="utf-8")
    r = bw.set_wlmaker("<Alt>v", command=CMD)
    assert r.state == bw.MANUAL
    assert cfg.read_text(encoding="utf-8") == broken
    assert not cfg.with_name(cfg.name + bw.BACKUP_SUFFIX).exists()


def test_wlmaker_missing_config_asks_for_manual_setup(_never_touch_the_desktop):
    assert bw.set_wlmaker("<Alt>v", command=CMD).state == bw.MANUAL


def test_orphan_comment_of_ours_is_cleaned(files):
    cfg, _ = files
    bw.set_wlmaker("<Alt>v", command=CMD)
    # big-wm-wprefs-sync removed the binding but not our comment above it:
    text = "\n".join(ln for ln in cfg.read_text(encoding="utf-8").splitlines() if "Alt+V" not in ln) + "\n"
    cfg.write_text(text, encoding="utf-8")
    bw.set_wlmaker("<Alt>v", command=CMD)
    assert cfg.read_text(encoding="utf-8").count("BigLinux TTS:") == 1


def test_reload_only_with_a_compositor_that_can_and_has_descriptors(files, monkeypatch):
    calls = []
    monkeypatch.setattr(bw, "reload_wlmaker", lambda pid: calls.append(pid) or True)
    r = bw.set_wlmaker("<Alt>v", compositor_pid=1234, command=CMD)
    assert r.state == bw.REGISTERED and calls == [1234]


def test_no_sighup_when_the_compositor_is_out_of_descriptors(monkeypatch):
    sent = []
    monkeypatch.setattr(bw, "wlmaker_can_reload", lambda pid: True)
    monkeypatch.setattr(bw.os, "kill", lambda *a: sent.append(a))
    monkeypatch.setattr(bw, "fd_headroom", lambda pid: 10)
    assert bw.real_reload_wlmaker(4242) is False and sent == []
    monkeypatch.setattr(bw, "fd_headroom", lambda pid: 500)
    monkeypatch.setattr(bw, "Path", lambda p: type("P", (), {"read_text": lambda self: "wlmaker\n"})())
    assert bw.real_reload_wlmaker(4242) is True and sent and sent[0][0] == 4242


def test_fd_headroom_reads_this_process():
    free = bw.fd_headroom(os.getpid())
    assert free is None or free > 0


# ── Window Maker root menu ───────────────────────────────────────────


def test_wmaker_item_next_to_the_clipboard_and_removed_byte_for_byte(files):
    _, menu = files
    r = bw.set_wmaker("<Alt>v", "Ler ou parar o texto selecionado", command=CMD)
    assert r.state == bw.PENDING  # wmaker not running here
    text = menu.read_text(encoding="utf-8")
    clip = text.index("big-wm-open clipboard")
    ours = text.index(f'("Ler ou parar o texto selecionado", SHORTCUT, "Mod1+v", EXEC, "{CMD}")')
    assert ours > clip and text.index("Sessão") > ours
    assert bw.set_wmaker("<Alt>v", "x", command=CMD).changed is False
    bw.set_wmaker("<Alt>n", "x", command=CMD)  # keeps the user's label and place
    assert '("Ler ou parar o texto selecionado", SHORTCUT, "Mod1+n"' in menu.read_text(encoding="utf-8")
    assert bw.set_wmaker(None).state == bw.REMOVED
    assert menu.read_text(encoding="utf-8") == MENU


@pytest.mark.parametrize("accel, culprit", [
    ("<Super>v", "Área de transferência"),
    ("<Super>l", "Bloquear tela"),  # inside a submenu
    ("<Alt>m", "Window Maker: MiniaturizeKey"),  # Window Maker's own default
])
def test_wmaker_conflicts(files, accel, culprit):
    _, menu = files
    r = bw.set_wmaker(accel, "x", command=CMD)
    assert r.state == bw.CONFLICT and culprit in r.conflicts
    assert menu.read_text(encoding="utf-8") == MENU


def test_wmaker_user_key_overrides_the_default(files):
    root = files[1].parent
    (root / "WindowMaker").write_text('{ MiniaturizeKey = "Mod4+m"; CloseKey = "Mod1+v"; }', encoding="utf-8")
    assert bw.set_wmaker("<Alt>m", "x", command=CMD).state == bw.PENDING
    assert bw.set_wmaker("<Alt>v", "x", command=CMD).state == bw.CONFLICT


# ── shortcut_service on Big WindowMaker ──────────────────────────────


def test_wayland_session_registers_in_wlmaker_only(files, monkeypatch):
    cfg, menu = files
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "wlmaker")
    st = shortcut_service.register("<Alt>v")
    assert st.registered is True and st.pending and st.desktop == "wlmaker"
    assert "Alt+V" in cfg.read_text(encoding="utf-8")
    # big-wm-wprefs-sync would regenerate the whole Wayland root menu.
    assert menu.read_text(encoding="utf-8") == MENU


def test_x11_session_registers_in_both(files, monkeypatch):
    cfg, menu = files
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "WindowMaker")
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/" + n)
    st = shortcut_service.register("<Alt>v")
    assert st.registered is True and st.desktop == "windowmaker"
    assert "Mod1+v" in menu.read_text(encoding="utf-8")
    assert "Alt+V" in cfg.read_text(encoding="utf-8")
    assert st.notes and st.notes[0].startswith("wlmaker (Wayland)")


def test_conflict_is_reported_with_the_other_action(files, monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "wlmaker")
    st = shortcut_service.register("<Alt>Tab")
    assert st.registered is False and st.conflicts and "TaskNext" in st.message


def test_unregister_removes_from_both(files, monkeypatch):
    cfg, menu = files
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "WindowMaker")
    shortcut_service.register("<Alt>v")
    results = shortcut_service.unregister_bigwm()
    assert [r.backend for r in results] == ["wlmaker", "windowmaker"]
    assert cfg.read_text(encoding="utf-8") == CONFIG and menu.read_text(encoding="utf-8") == MENU


# ── capture ──────────────────────────────────────────────────────────


@pytest.fixture
def wayland(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(cs.shutil, "which", lambda n: "/usr/bin/" + n)
    calls = []
    answers = {}

    def run(args, max_chars):
        calls.append(args)
        text = answers.get(tuple(args), "")
        return cs.ClipboardResult(text, bool(text), "" if text else "empty")

    monkeypatch.setattr(cs, "_run_capture", run)
    monkeypatch.setattr(cs, "_primary_supported", lambda: True)
    return calls, answers


WL_PRIMARY = ("wl-paste", "--primary", "--no-newline", "--type", "text")
WL_CLIP = ("wl-paste", "--no-newline", "--type", "text")
X_PRIMARY = ("xsel", "--primary", "-o")


def test_wayland_reads_the_wayland_selection(wayland, monkeypatch):
    calls, answers = wayland
    monkeypatch.setattr(cs, "_xwayland_focus", lambda: None)
    answers[WL_PRIMARY] = "Olá, ação 🎉\nlinha 2"
    r = cs.get_selected_text()
    assert r.text == "Olá, ação 🎉\nlinha 2" and r.source == cs.SRC_WAYLAND_PRIMARY
    assert cs.describe(r) == "19 chars from wayland-primary"  # never the text


def test_focused_xwayland_program_is_read_from_x11(wayland, monkeypatch):
    calls, answers = wayland
    monkeypatch.setattr(cs, "_xwayland_focus", lambda: X11Focus(0x3600001, True, 0x3600002, True))
    answers[X_PRIMARY] = "texto do xterm"
    answers[WL_PRIMARY] = "seleção antiga do Wayland"
    r = cs.get_selected_text()
    assert r.text == "texto do xterm" and r.source == cs.SRC_XWAYLAND_PRIMARY


def test_wayland_program_focused_ignores_old_x11_selection(wayland, monkeypatch):
    calls, answers = wayland
    monkeypatch.setattr(cs, "_xwayland_focus", lambda: X11Focus(0x200005, False, 0x3600002, True))
    answers[X_PRIMARY] = "seleção antiga do xterm"
    answers[WL_PRIMARY] = "seleção atual"
    assert cs.get_selected_text().text == "seleção atual"
    assert list(X_PRIMARY) not in calls


def test_nothing_selected_never_reads_the_old_clipboard(wayland, monkeypatch):
    calls, answers = wayland
    monkeypatch.setattr(cs, "_xwayland_focus", lambda: None)
    answers[WL_CLIP] = "senha copiada há uma hora"
    r = cs.get_selected_text()
    assert r.error == cs.NO_SELECTION and not r.text and list(WL_CLIP) not in calls
    r = cs.get_selected_text(clipboard_fallback=True)
    assert r.text == "senha copiada há uma hora" and r.source == cs.SRC_CLIPBOARD


def test_compositor_without_primary_selection_is_reported(wayland, monkeypatch):
    monkeypatch.setattr(cs, "_xwayland_focus", lambda: None)
    monkeypatch.setattr(cs, "_primary_supported", lambda: False)
    assert cs.get_selected_text().error == cs.PRIMARY_UNSUPPORTED


def test_lazy_xwayland_is_not_started_to_check_focus(monkeypatch):
    from services import x11_selection

    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(session_info, "xwayland_running", lambda env=None: False)
    monkeypatch.setattr(x11_selection, "focus", lambda d: pytest.fail("connected to X"))
    assert cs._xwayland_focus() is None


# ── Wayland registry probe ───────────────────────────────────────────


def _fake_compositor(path: Path, globals_: list[tuple[str, int]]):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        conn.recv(64)  # get_registry + sync
        out = b""
        for i, (iface, ver) in enumerate(globals_, 1):
            s = iface.encode() + b"\0"
            body = struct.pack("<II", i, len(s)) + s + b"\0" * ((4 - len(s) % 4) % 4) + struct.pack("<I", ver)
            out += struct.pack("<II", 2, ((8 + len(body)) << 16) | 0) + body
        out += struct.pack("<III", 3, (12 << 16) | 0, 1)  # wl_callback.done
        conn.sendall(out)
        conn.close()
        srv.close()

    threading.Thread(target=serve, daemon=True).start()


def test_registry_probe_lists_the_compositor_protocols(tmp_path):
    sock = tmp_path / "wayland-test"
    _fake_compositor(sock, [("wl_seat", 9), ("zwp_primary_selection_device_manager_v1", 1),
                            ("zwlr_data_control_manager_v1", 2)])
    env = {"WAYLAND_DISPLAY": str(sock)}
    assert wayland_globals.selection_support(env) == {
        "reachable": True, "primary": True, "data_control": "zwlr_data_control_manager_v1"}


def test_registry_probe_without_primary_selection(tmp_path):
    sock = tmp_path / "wayland-test"
    _fake_compositor(sock, [("wl_seat", 9)])
    assert wayland_globals.selection_support({"WAYLAND_DISPLAY": str(sock)})["primary"] is False
    assert wayland_globals.selection_support({"WAYLAND_DISPLAY": str(tmp_path / "none")})["reachable"] is False


# ── fast path of the shortcut command ────────────────────────────────


def test_speak_command_closes_inherited_descriptors(tmp_path):
    """biglinux-tts-speak must not pass the compositor's pipes on to the app."""
    import subprocess

    script = Path(__file__).resolve().parents[1] / "usr" / "bin" / "biglinux-tts-speak"
    text = script.read_text()
    loop = text[text.index("for fd in"):text.index("done", text.index("for fd in")) + 4]
    probe = tmp_path / "probe.sh"
    probe.write_text("#!/bin/bash\n" + loop + "\nls /proc/$$/fd\n")
    r, w = os.pipe()
    out = subprocess.run(["bash", str(probe)], pass_fds=(r, w), capture_output=True, text=True).stdout.split()
    os.close(r)
    os.close(w)
    assert str(r) not in out and str(w) not in out and {"0", "1", "2"} <= set(out)


def test_diagnose_flags_a_binding_that_runs_another_command(files, monkeypatch):
    """Installed over a source checkout: the key is right, the command is not."""
    from config import AppSettings
    from services import diagnostics
    from services.desktop_integration_service import DesktopIntegrationService as DIS

    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "wlmaker")
    bw.set_wlmaker("<Alt>v", command="/home/u/checkout/usr/bin/biglinux-tts-speak")
    monkeypatch.setattr(DIS, "_exec_path_for_speak", staticmethod(lambda: CMD))
    settings = AppSettings()
    data = diagnostics.collect_integration(settings)
    assert data["shortcut"]["wlmaker"]["commands"] == ["/home/u/checkout/usr/bin/biglinux-tts-speak"]
    assert any("--repair-shortcut" in p for p in data["problems"])
    bw.set_wlmaker("<Alt>v", command=CMD)
    assert not any("--repair-shortcut" in p for p in diagnostics.collect_integration(settings)["problems"])
