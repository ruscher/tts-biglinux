"""Pytest config — put the app package dir on sys.path so `services`,
`config`, `ui`, `utils` import like they do at runtime."""
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "usr" / "share" / "biglinux" / "tts-biglinux"
sys.path.insert(0, str(APP_DIR))

import atexit  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

import pytest  # noqa: E402

import config  # noqa: E402
from services import history_service  # noqa: E402

# Session-wide fallback: worker threads of a test may still run after its
# per-test redirection below is undone; they must find a scratch folder too,
# never the user's real history or settings.
_SESSION_HOME = Path(tempfile.mkdtemp(prefix="tts-tests-"))
atexit.register(shutil.rmtree, _SESSION_HOME, True)
history_service._MUSIC_DIR = _SESSION_HOME / "Music"
history_service._trash = history_service._remove
config.CONFIG_DIR = _SESSION_HOME / ".config" / "biglinux-tts"
config.SETTINGS_FILE = config.CONFIG_DIR / "settings.json"
config.LEGACY_CONFIG_DIR = _SESSION_HOME / ".config" / "tts-biglinux"


@pytest.fixture(autouse=True)
def _never_touch_user_data(tmp_path_factory, monkeypatch):
    """Every test gets its own history folder and settings file.

    A test that enables the history must never write into the user's real
    Music/tts-biglinux (it happened once), nor fill their Trash, nor change
    their settings.json.
    """
    home = tmp_path_factory.mktemp("user")
    monkeypatch.setattr(history_service, "_MUSIC_DIR", home / "Music")
    # Deleting moves files to the desktop Trash: never the user's real one.
    monkeypatch.setattr(history_service, "_trash", history_service._remove)
    monkeypatch.setattr(config, "CONFIG_DIR", home / ".config" / "biglinux-tts")
    monkeypatch.setattr(config, "SETTINGS_FILE", home / ".config" / "biglinux-tts" / "settings.json")
    monkeypatch.setattr(config, "LEGACY_CONFIG_DIR", home / ".config" / "tts-biglinux")


@pytest.fixture(autouse=True)
def _never_touch_the_desktop(tmp_path_factory, monkeypatch):
    """No test edits the real session's shortcut files or signals it.

    The suite may run inside a Big WindowMaker session: registration would
    otherwise write ~/.config/wlmaker/Config.plist and ~/GNUstep/Defaults
    and send SIGHUP to the running wlmaker. Session detection is reduced to
    the environment (a test sets the variables it needs), the config roots
    point to a scratch folder and no compositor is ever signalled.
    """
    from services import bigwm_shortcuts, session_info

    root = tmp_path_factory.mktemp("desktop")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / ".config"))
    monkeypatch.setenv("GNUSTEP_USER_ROOT", str(root / "GNUstep"))
    monkeypatch.setattr(session_info, "wayland_compositor_pid", lambda env=None: None)
    monkeypatch.setattr(session_info, "_own_processes", lambda names: {})
    # The real one stays reachable for the test of its own guards.
    monkeypatch.setattr(bigwm_shortcuts, "real_reload_wlmaker", bigwm_shortcuts.reload_wlmaker, raising=False)
    monkeypatch.setattr(bigwm_shortcuts, "reload_wlmaker", lambda pid: False)
    yield root

