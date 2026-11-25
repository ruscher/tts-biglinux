"""The application side of desktop integration (real app object, no sound):
the "speak" action of biglinux-tts-speak's fast path, what is recorded about
a capture (never the text), the tray helper's recovery, and the labels of
the Desktop integration panel. Needs a display; skipped otherwise.
"""
import importlib

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, Gtk  # noqa: E402

if not Gtk.init_check() or Gdk.Display.get_default() is None:
    pytest.skip("no display for GTK", allow_module_level=True)

from fake_engines import use_fakes  # noqa: E402

application = importlib.import_module("application")
cs = importlib.import_module("services.clipboard_service")
panel = importlib.import_module("ui.integration_panel")


@pytest.fixture
def app(tmp_path, monkeypatch):
    use_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(application.TTSApplication, "_show_dbus_notification", lambda *a, **k: None)
    a = application.TTSApplication()
    a.settings.speech.backend = "rhvoice"
    a.settings.speech.voice_id = "Leticia-F123"
    return a


def test_speak_request_records_the_shortcut_use(app, monkeypatch):
    calls, heard = [], []
    monkeypatch.setattr(app, "_on_tray_speak", lambda: calls.append(1))
    app.add_integration_listener(lambda: heard.append(app.last_speak_request))
    app.speak_request()
    assert calls == [1] and app.last_speak_request > 0 and heard
    app._release_speak_hold()


def test_capture_is_recorded_without_the_text(app, monkeypatch):
    monkeypatch.setattr(cs, "get_selected_text",
                        lambda max_chars=0, **kw: cs.ClipboardResult("segredo", True, "", cs.SRC_XWAYLAND_PRIMARY))
    app._on_tray_speak()
    import time

    from gi.repository import GLib

    end = time.monotonic() + 5
    while not app.last_capture and time.monotonic() < end:
        GLib.MainContext.default().iteration(False)
    assert app.last_capture == "7 chars from xwayland-primary"
    assert "segredo" not in app.last_capture
    app.tts_service.stop()


def test_clipboard_option_reaches_the_capture(app, monkeypatch):
    seen = []
    monkeypatch.setattr(cs, "get_selected_text",
                        lambda max_chars=0, **kw: seen.append(kw) or cs.ClipboardResult("", False, cs.NO_SELECTION))
    app.settings.shortcut.clipboard_fallback = True
    app._on_tray_speak()
    import time

    from gi.repository import GLib

    end = time.monotonic() + 5
    while not seen and time.monotonic() < end:
        GLib.MainContext.default().iteration(False)
    assert seen == [{"clipboard_fallback": True}]


class FakeTray:
    def __init__(self, crashed, error=""):
        self.crashed, self.last_error = crashed, error
        self.unregistered = False

    def unregister(self):
        self.unregistered = True


def test_crashed_tray_helper_is_restarted_a_few_times(app, monkeypatch):
    timers = []
    monkeypatch.setattr(application.GLib, "timeout_add_seconds", lambda s, f: timers.append(s) or 7)
    monkeypatch.setattr(app, "release", lambda: None)
    for _ in range(4):
        app._tray = FakeTray(True, "Tray crashed: boom")
        app._on_tray_lost()
        app._tray_restart_id = 0
    assert timers == [2, 6, 18]  # then it gives up
    assert app.tray_state == "unavailable"


def test_no_tray_host_waits_for_one_instead_of_looping(app, monkeypatch):
    watched = []
    monkeypatch.setattr(app, "release", lambda: None)
    monkeypatch.setattr(app, "_watch_tray_host", lambda: watched.append(1))
    monkeypatch.setattr(application.GLib, "timeout_add_seconds", lambda *a: pytest.fail("restart loop"))
    app._tray = FakeTray(False, "No system tray on this desktop. Tray icon is disabled.")
    app._on_tray_lost()
    assert watched == [1] and app.tray_state == "unavailable" and app._tray is None


def test_panel_labels_never_contain_text():
    assert panel.capture_label("44 chars from wayland-primary").count("44") == 1
    assert panel.capture_label("") == ""
    assert panel.desktop_label("wlmaker", "wayland") == "Big WindowMaker (Wayland)"
    assert panel.desktop_label("windowmaker", "x11") == "Big WindowMaker (X11)"
    assert "Wayland" in panel.desktop_label("kde", "wayland")


def test_panel_builds_and_follows_the_app(app):
    from services.shortcut_service import ShortcutStatus

    group = panel.IntegrationGroup(app, app.settings_service)
    app.shortcut_status = ShortcutStatus(accel="<Alt>v", registered=False, message="taken", conflicts=["x"])
    app.tray_state, app.tray_detail = "unavailable", "No system tray on this desktop."
    group.refresh()
    assert group._fix_button.get_visible()
    assert group._shortcut_row.get_subtitle() == "taken"
