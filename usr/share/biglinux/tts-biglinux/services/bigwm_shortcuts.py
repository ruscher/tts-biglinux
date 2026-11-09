"""
Global shortcut on Big WindowMaker: wlmaker (Wayland) and Window Maker (X11).

Neither has a shortcut API; each has a file it already uses for its own
shortcuts, and that is where the shortcut goes — the way Big WindowMaker
itself sets Super+V for the clipboard history:

* wlmaker: an entry in ``KeyBindings`` of its Config.plist,
      "Alt+V" = (ShellExecute, "biglinux-tts-speak");
  applied by a configuration reload (SIGHUP, Big WindowMaker's wlmaker), or
  at the next login with a wlmaker that cannot reload.
* Window Maker: an item of the root menu (WMRootMenu) with a SHORTCUT,
      ("Read or stop the selected text", SHORTCUT, "Mod1+v", EXEC, biglinux-tts-speak)
  bound while the menu is closed; Window Maker notices the file change and
  rebinds by itself. big-wm-wprefs-sync carries root-menu shortcuts over to
  wlmaker's KeyBindings — the same entry as above, so the two never disagree.

Only the entry that runs biglinux-tts-speak is ever written or removed; the
rest of the file is kept byte for byte (comments included). Before writing:
the other shortcuts are checked for the same key (conflict → nothing is
written), the result is re-parsed with the rules of the program that reads
it, the previous file is kept as ``<file>.bak-biglinux-tts``, and the file is
replaced atomically under big-wm-wprefs-sync's lock. A file that cannot be
parsed is never touched — an invalid Config.plist stops wlmaker at login.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import logging
import os
import re
import shlex
import signal
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from services import plist
from services.plist import PlistError

logger = logging.getLogger(__name__)

SPEAK_COMMAND = "biglinux-tts-speak"
BACKUP_SUFFIX = ".bak-biglinux-tts"

# Outcomes (WMResult.state).
REGISTERED = "registered"  # in the file and in effect
PENDING = "pending-restart"  # in the file; the compositor reads it at the next login
CONFLICT = "conflict"  # the key is taken by another action: nothing written
MANUAL = "manual"  # no file we can safely edit (see message)
ERROR = "error"  # could not write; nothing changed
REMOVED = "removed"


@dataclass
class WMResult:
    backend: str  # "wlmaker" | "windowmaker"
    state: str
    path: Path | None = None
    conflicts: list[str] = field(default_factory=list)
    detail: str = ""  # untranslated, for logs and diagnostics
    changed: bool = False
    backup: Path | None = None
    key: str = ""  # the key as written in the file ("Alt+V", "Mod1+v")

    @property
    def ok(self) -> bool:
        return self.state in (REGISTERED, PENDING)


# ── keys ─────────────────────────────────────────────────────────────

_GTK_MODS = {
    "control": "Ctrl", "ctrl": "Ctrl", "primary": "Ctrl",
    "alt": "Alt", "mod1": "Alt",
    "shift": "Shift",
    "super": "Logo", "meta": "Logo", "hyper": "Logo", "mod4": "Logo",
}
# wlmaker (src/input/keyboard.c) and Window Maker modifier names.
_WL_MODS = {"shift": "Shift", "ctrl": "Ctrl", "alt": "Alt", "logo": "Logo",
            "mod2": "Mod2", "mod3": "Mod3", "mod5": "Mod5"}
_WM_MODS = {"control": "Ctrl", "ctrl": "Ctrl", "mod1": "Alt", "alt": "Alt", "meta": "Alt",
            "shift": "Shift", "mod4": "Logo", "super": "Logo", "hyper": "Logo",
            "mod2": "Mod2", "mod3": "Mod3", "mod5": "Mod5"}
_ORDER = ("Ctrl", "Alt", "Shift", "Logo", "Mod2", "Mod3", "Mod5")


Combo = tuple[frozenset, str]  # (modifiers, keysym name lowercase)


def parse_gtk_accel(accel: str) -> tuple[frozenset, str] | None:
    """``<Control><Alt>v`` → ({"Ctrl","Alt"}, "v"); None if not a key."""
    mods = set()
    rest = accel.strip()
    while rest.startswith("<"):
        end = rest.find(">")
        if end < 0:
            return None
        name = rest[1:end].lower()
        if name not in _GTK_MODS:
            return None
        mods.add(_GTK_MODS[name])
        rest = rest[end + 1:]
    if not rest or "<" in rest or "+" in rest and len(rest) > 1:
        return None
    return frozenset(mods), rest


def _split(spec: str, names: dict) -> Combo | None:
    mods, key = set(), None
    for part in spec.split("+"):
        p = part.strip()
        if not p:
            return None
        m = names.get(p.lower())
        if m:
            mods.add(m)
        elif key is None:
            key = p
        else:
            return None
    if key is None or key.lower() == "none":
        return None
    return frozenset(mods), key.lower()


def wlmaker_combo(spec: str) -> Combo | None:
    return _split(spec, _WL_MODS)


def wmaker_combo(spec: str) -> Combo | None:
    return _split(spec, _WM_MODS)


def _key_name(key: str) -> str:
    return key.upper() if len(key) == 1 else key


def to_wlmaker_key(accel: str) -> str:
    """``<Alt>v`` → ``Alt+V`` (the style of Big WindowMaker's Config.plist)."""
    parsed = parse_gtk_accel(accel)
    if not parsed:
        return ""
    mods, key = parsed
    return "+".join([m for m in _ORDER if m in mods] + [_key_name(key)])


def to_wmaker_key(accel: str) -> str:
    """``<Alt>v`` → ``Mod1+v`` (Window Maker's names, as in WMRootMenu)."""
    parsed = parse_gtk_accel(accel)
    if not parsed:
        return ""
    mods, key = parsed
    names = {"Ctrl": "Control", "Alt": "Mod1", "Shift": "Shift", "Logo": "Mod4"}
    return "+".join([names[m] for m in _ORDER if m in mods] + [key])


def combo_of(accel: str) -> Combo | None:
    parsed = parse_gtk_accel(accel)
    return (parsed[0], parsed[1].lower()) if parsed else None


class _Xkb:
    lib = None

    @classmethod
    def valid(cls, name: str) -> bool:
        """The keysym exists (the check wlmaker does when it binds)."""
        if cls.lib is None:
            try:
                cls.lib = ctypes.CDLL(ctypes.util.find_library("xkbcommon") or "libxkbcommon.so.0")
                cls.lib.xkb_keysym_from_name.restype = ctypes.c_uint32
                cls.lib.xkb_keysym_from_name.argtypes = [ctypes.c_char_p, ctypes.c_int]
            except OSError:
                cls.lib = False
        if not cls.lib:
            return bool(re.fullmatch(r"[A-Za-z0-9_]+", name))
        return cls.lib.xkb_keysym_from_name(name.encode(), 1) != 0  # case-insensitive


def _runs_speak(command: str) -> bool:
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return bool(argv) and os.path.basename(argv[0]) == SPEAK_COMMAND


def _combo_text(combo: Combo) -> str:
    mods, key = combo
    return "+".join([m for m in _ORDER if m in mods] + [_key_name(key)])


# ── files ────────────────────────────────────────────────────────────


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _gnustep_defaults() -> Path:
    root = os.environ.get("GNUSTEP_USER_ROOT")
    return (Path(root).expanduser() if root else Path.home() / "GNUstep") / "Defaults"


class _Lock:
    """big-wm-wprefs-sync's lock, when it exists: no sync runs while we write.

    Never created here (no file of ours in its folder); given up after a few
    seconds rather than blocking registration forever.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.fd = None

    def __enter__(self):
        if self.path is None or not self.path.exists():
            return self
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_CLOEXEC)
        except OSError:
            return self
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() > deadline:
                    logger.warning("big-wm-wprefs-sync lock busy; writing anyway")
                    return self
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)  # releases the flock
        return False


def _atomic_replace(path: Path, original: str, text: str) -> Path:
    """Back up ``original`` and replace ``path`` with ``text`` atomically.

    Refuses when the file changed since it was read (another program wrote
    it meanwhile): the caller reads again rather than undo that change.
    """
    if path.read_text(encoding="utf-8") != original:
        raise OSError(f"{path} changed while it was being edited")
    st = path.stat()
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    _write_file(backup, original, st.st_mode & 0o777)
    _write_file(path, text, st.st_mode & 0o777)
    return backup


def _write_file(path: Path, text: str, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + text[end:]
    return text


# ── wlmaker ──────────────────────────────────────────────────────────

_WL_COMMENT = "// BigLinux TTS: read or stop the selected text"


def wlmaker_config_path(compositor_pid: int | None = None) -> Path:
    """The Config.plist the running wlmaker read (``--config_file``, or
    ``$XDG_CONFIG_HOME/wlmaker/Config.plist``)."""
    if compositor_pid:
        try:
            argv = Path(f"/proc/{compositor_pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
            for i, a in enumerate(argv):
                if a.startswith("--config_file="):
                    return Path(a.split("=", 1)[1]).expanduser()
                if a == "--config_file" and i + 1 < len(argv):
                    return Path(argv[i + 1]).expanduser()
        except OSError:
            pass
    return _config_home() / "wlmaker" / "Config.plist"


def wlmaker_can_reload(pid: int) -> bool:
    """Big WindowMaker's wlmaker reloads on SIGHUP; others would exit.

    Reads the binary that runs (/proc/PID/exe), not the installed one: after
    an update the session still runs the old one.
    """
    try:
        with open(f"/proc/{pid}/exe", "rb") as f:
            return b"ReloadConfiguration" in f.read()
    except OSError:
        return False


def _wlmaker_bindings(text: str) -> plist.Node:
    root = plist.check(text, "wlmaker")
    if root.kind != "dict":
        raise PlistError("Config.plist is not a dictionary")
    entry = root.entry("KeyBindings")
    if entry is None or entry.value.kind != "dict":
        raise PlistError("Config.plist has no KeyBindings dictionary")
    return entry.value


def _wl_action(node: plist.Node) -> tuple[str, str]:
    if node.kind == "str":
        return str(node.value), ""
    if node.kind == "array" and node.items and node.items[0].kind == "str":
        arg = node.items[1].value if len(node.items) > 1 and node.items[1].kind == "str" else ""
        return str(node.items[0].value), str(arg)
    return "", ""


def _wl_is_ours(entry: plist.Entry) -> bool:
    action, arg = _wl_action(entry.value)
    return action in ("ShellExecute", "Execute") and _runs_speak(arg)


def _wl_describe(entry: plist.Entry) -> str:
    action, arg = _wl_action(entry.value)
    return f"{entry.key.value} → {action}{' ' + arg if arg else ''}"


def wlmaker_bound(path: Path) -> list[tuple[str, str]]:
    """(key, command) of our bindings in the file — for diagnostics; raises on errors."""
    kb = _wlmaker_bindings(path.read_text(encoding="utf-8"))
    return [(str(e.key.value), _wl_action(e.value)[1]) for e in kb.entries if _wl_is_ours(e)]


def wmaker_bound(path: Path) -> list[tuple[str, str]]:
    """(SHORTCUT, command) of our root-menu items — for diagnostics; raises on errors."""
    menu = _wm_menu(path.read_text(encoding="utf-8"))
    out = []
    for item, _ in _wm_walk(menu):
        if _wm_is_ours(item):
            cmd, sc = _wm_item_command(item)
            out.append((str(sc.value) if sc is not None else "", cmd))
    return out


def wlmaker_status(path: Path) -> tuple[list[str], list[str]]:
    """(our keys, all keys) in the file — for diagnostics; raises on errors."""
    kb = _wlmaker_bindings(path.read_text(encoding="utf-8"))
    return [e.key.value for e in kb.entries if _wl_is_ours(e)], [e.key.value for e in kb.entries]


def _wl_entry_span(text: str, entry: plist.Entry) -> tuple[int, int]:
    """The entry's whole line(s), with our comment line above it, if any."""
    start = plist.line_start(text, entry.start)
    if text[start:entry.start].strip():
        start = entry.start  # shares the line with something else
    end = entry.end
    nl = text.find("\n", end)
    if nl >= 0 and not text[end:nl].strip():
        end = nl + 1
    prev = text.rfind("\n", 0, max(start - 1, 0))
    above = text[prev + 1:start]
    if start > 0 and above.strip() == _WL_COMMENT:
        start = prev + 1
    return start, end


def _wl_edit(text: str, key: str | None, command: str = SPEAK_COMMAND) -> tuple[str, list[str]]:
    """``text`` with our binding set to ``key`` (None: removed).

    Returns (new text, conflicts). With conflicts the text is unchanged.
    """
    kb = _wlmaker_bindings(text)
    ours = [e for e in kb.entries if _wl_is_ours(e)]
    if key is not None:
        combo = wlmaker_combo(key)
        conflicts = [_wl_describe(e) for e in kb.entries
                     if not _wl_is_ours(e) and wlmaker_combo(str(e.key.value)) == combo]
        if conflicts:
            return text, conflicts
        if len(ours) == 1 and ours[0].key.value == key and _wl_action(ours[0].value) == ("ShellExecute", command):
            return text, []  # already exactly right
    new = _remove_spans(text, [_wl_entry_span(text, e) for e in ours])
    # Our comment left alone (big-wm-wprefs-sync deletes a binding that came
    # from the Window Maker menu when the menu item goes away).
    new = re.sub(rf"(?m)^[ \t]*{re.escape(_WL_COMMENT)}[ \t]*\n", "", new)
    if key is None:
        return new, []

    # Insert as the first binding, indented like the others.
    kb = _wlmaker_bindings(new)
    first = kb.entries[0] if kb.entries else None
    indent = plist.line_indent(new, first.start) if first else plist.line_indent(new, kb.start) + "    "
    line = f'{indent}{_WL_COMMENT}\n{indent}{plist.quote(key)} = (ShellExecute, {plist.quote(command)});\n'
    nl = new.find("\n", kb.start)
    if nl < 0 or nl > (first.start if first else kb.end):
        # "{" shares its line with the first entry: start a new line.
        pos = kb.start + 1
        line = "\n" + line
    else:
        pos = nl + 1
    return new[:pos] + line + new[pos:], []


def _wl_verify(before: str, after: str, key: str | None) -> None:
    """The edit changed our binding only, and wlmaker can read the result."""
    old, new = _wlmaker_bindings(before), _wlmaker_bindings(after)
    others_old = [(e.key.value, e.value.to_py()) for e in old.entries if not _wl_is_ours(e)]
    others_new = [(e.key.value, e.value.to_py()) for e in new.entries if not _wl_is_ours(e)]
    if others_old != others_new:
        raise PlistError("other key bindings would change")
    ours = [e.key.value for e in new.entries if _wl_is_ours(e)]
    if ours != ([key] if key else []):
        raise PlistError(f"unexpected result: {ours}")
    # Everything outside KeyBindings stays as it was.
    root_old = {e.key.value: e.value.to_py() for e in plist.check(before, "wlmaker").entries if e.key.value != "KeyBindings"}
    root_new = {e.key.value: e.value.to_py() for e in plist.check(after, "wlmaker").entries if e.key.value != "KeyBindings"}
    if root_old != root_new:
        raise PlistError("other settings would change")


def fd_headroom(pid: int) -> int | None:
    """Descriptors the process can still open (soft limit − open); None: unknown."""
    try:
        open_now = len(os.listdir(f"/proc/{pid}/fd"))
        for line in Path(f"/proc/{pid}/limits").read_text().splitlines():
            if line.startswith("Max open files"):
                soft = line.split()[3]
                return None if soft == "unlimited" else int(soft) - open_now
    except (OSError, ValueError, IndexError):
        pass
    return None


# A reload re-reads three files and sends every client a new keymap (one
# descriptor each). wlmaker before the CLOEXEC fix leaks pipes; near its
# limit a reload fails half-way and drops clients (the dock's tray did).
MIN_FD_HEADROOM = 64


def reload_wlmaker(pid: int | None) -> bool:
    """Ask the session's wlmaker to re-read its configuration (SIGHUP)."""
    if not pid or not wlmaker_can_reload(pid):
        return False
    headroom = fd_headroom(pid)
    if headroom is not None and headroom < MIN_FD_HEADROOM:
        logger.warning("wlmaker has only %d free file descriptors: not reloading it now "
                       "(the shortcut applies at the next login)", headroom)
        return False
    try:
        if Path(f"/proc/{pid}/comm").read_text().strip() != "wlmaker":
            return False
        os.kill(pid, signal.SIGHUP)
        return True
    except OSError as e:
        logger.warning("Could not ask wlmaker to reload: %s", e)
        return False


def _compositor_started(pid: int) -> float | None:
    try:
        return Path(f"/proc/{pid}").stat().st_mtime
    except OSError:
        return None


def set_wlmaker(accel: str | None, compositor_pid: int | None = None, path: Path | None = None,
                command: str = SPEAK_COMMAND) -> WMResult:
    """Bind ``accel`` (GTK form; None removes it) in wlmaker's KeyBindings.

    ``compositor_pid``: the running wlmaker to reload (None: none running,
    e.g. while in the X11 session). Blocking (file I/O): worker thread.
    """
    path = path or wlmaker_config_path(compositor_pid)
    res = WMResult("wlmaker", ERROR, path)
    key = None
    if accel is not None:
        key = to_wlmaker_key(accel)
        res.key = key
        combo = wlmaker_combo(key) if key else None
        if not combo or not _Xkb.valid(combo[1]):
            res.detail = f"key not valid for wlmaker: {accel!r}"
            return res
    if not path.is_file():
        res.state = MANUAL
        res.detail = f"{path} does not exist (wlmaker uses its built-in or system configuration)"
        return res
    with _Lock(path.parent / ".wprefs-sync.json.lock"):
        try:
            original = path.read_text(encoding="utf-8")
            new, conflicts = _wl_edit(original, key, command)
        except (OSError, UnicodeDecodeError, PlistError) as e:
            res.state = MANUAL
            res.detail = f"{path} not edited: {e}"
            return res
        if conflicts:
            res.state = CONFLICT
            res.conflicts = conflicts
            res.detail = f"{key} already bound: {'; '.join(conflicts)}"
            return res
        if new != original:
            try:
                _wl_verify(original, new, key)
                res.backup = _atomic_replace(path, original, new)
            except (OSError, PlistError) as e:
                res.detail = f"not written: {e}"
                return res
            res.changed = True

    if accel is None:
        res.state = REMOVED
    else:
        res.state = REGISTERED
    if res.changed:
        if reload_wlmaker(compositor_pid):
            res.detail = "written; wlmaker reloaded its configuration"
        else:
            res.state = PENDING if accel is not None else REMOVED
            res.detail = "written; takes effect when wlmaker starts again"
    elif compositor_pid:
        # Unchanged file: in effect, unless it changed after wlmaker started
        # and this wlmaker cannot reload (then the next login applies it).
        started = _compositor_started(compositor_pid)
        if accel is not None and started and path.stat().st_mtime > started and not wlmaker_can_reload(compositor_pid):
            res.state = PENDING
            res.detail = "in the file; takes effect when wlmaker starts again"
    logger.info("wlmaker shortcut %s: %s (%s)", key, res.state, res.detail or path)
    return res


# ── Window Maker (X11) ───────────────────────────────────────────────

# Window Maker's own shortcuts that have a key by default (src/defaults.c);
# a user's WindowMaker domain file overrides them.
_WMAKER_DEFAULT_KEYS = {
    "RootMenuKey": "F12", "WindowListKey": "F11", "WindowMenuKey": "Control+Escape",
    "MiniaturizeKey": "Mod1+M", "HideKey": "Mod1+H", "RaiseKey": "Mod1+Up",
    "LowerKey": "Mod1+Down", "FocusNextKey": "Mod1+Tab", "FocusPrevKey": "Mod1+Shift+Tab",
    "NextWorkspaceKey": "Mod1+Control+Right", "PrevWorkspaceKey": "Mod1+Control+Left",
    **{f"Workspace{n}Key": f"Mod1+{n % 10}" for n in range(1, 11)},
}


def wmaker_menu_path() -> Path:
    return _gnustep_defaults() / "WMRootMenu"


def _wm_item_command(item: plist.Node) -> tuple[str, plist.Node | None]:
    """(command, SHORTCUT value node) of a root-menu item, '' if no EXEC."""
    if item.kind != "array":
        return "", None
    vals = item.items
    shortcut = None
    cmd = ""
    i = 1
    while i < len(vals):
        v = vals[i]
        word = v.value if v.kind == "str" else None
        if word == "SHORTCUT" and i + 1 < len(vals):
            shortcut = vals[i + 1]
            i += 2
            continue
        if word in ("EXEC", "SHEXEC") and i + 1 < len(vals) and vals[i + 1].kind == "str":
            cmd = str(vals[i + 1].value)
        i += 1
    return cmd, shortcut


def _wm_walk(menu: plist.Node, depth: int = 0):
    """Every item of the menu tree, as (item node, parent menu node)."""
    if menu.kind != "array" or depth > 20:
        return
    for item in menu.items[1:]:
        if item.kind != "array":
            continue
        yield item, menu
        # A submenu is (title, item, item...): its second element is a list.
        if len(item.items) > 1 and item.items[1].kind == "array":
            yield from _wm_walk(item, depth + 1)


def _wm_menu(text: str) -> plist.Node:
    root = plist.check(text, "wings")
    if root.kind != "array" or not root.items or root.items[0].kind != "str":
        raise PlistError("WMRootMenu is not a menu list (a menu file path is used instead)")
    return root


def _wm_is_ours(item: plist.Node) -> bool:
    cmd, _ = _wm_item_command(item)
    return bool(cmd) and _runs_speak(cmd)


def _wm_domain_conflicts(combo: Combo) -> list[str]:
    keys = dict(_WMAKER_DEFAULT_KEYS)
    path = _gnustep_defaults() / "WindowMaker"
    try:
        root = plist.parse(path.read_text(encoding="utf-8"), "wings")
        if root is not None and root.kind == "dict":
            for e in root.entries:
                name = str(e.key.value)
                if name.endswith("Key") and name != "ModifierKey" and e.value.kind == "str":
                    keys[name] = str(e.value.value)
    except (OSError, UnicodeDecodeError, PlistError):
        pass
    return [f"Window Maker: {name}" for name, spec in keys.items() if wmaker_combo(spec) == combo]


def _wm_edit(text: str, key: str | None, label: str, command: str = SPEAK_COMMAND) -> tuple[str, list[str]]:
    menu = _wm_menu(text)
    ours = [item for item, _ in _wm_walk(menu) if _wm_is_ours(item)]
    if key is not None:
        combo = wmaker_combo(key)
        conflicts = []
        for item, _ in _wm_walk(menu):
            _, sc = _wm_item_command(item)
            if not _wm_is_ours(item) and sc is not None and sc.kind == "str" and wmaker_combo(str(sc.value)) == combo:
                conflicts.append(f"{item.items[0].value}")
        conflicts += _wm_domain_conflicts(combo)
        if conflicts:
            return text, conflicts
        if len(ours) == 1:
            cmd, sc = _wm_item_command(ours[0])
            if sc is not None and sc.value == key and cmd == command:
                return text, []  # already exactly right

    if key is not None and ours:
        # Keep the user's position and label; rewrite the rest of the item.
        item = ours[0]
        title = text[item.items[0].start:item.items[0].end]
        new_item = f"({title}, SHORTCUT, {plist.quote(key)}, EXEC, {plist.quote(command)})"
        text = text[:item.start] + new_item + text[item.end:]
        rest = [i for i, _ in _wm_walk(_wm_menu(text)) if _wm_is_ours(i)][1:]
        return _remove_items(text, rest), []

    text = _remove_items(text, ours)
    if key is None:
        return text, []

    menu = _wm_menu(text)
    top = menu.items[1:]
    anchor = next((i for i in top if _wm_item_command(i)[0].strip() == "big-wm-open clipboard"), None)
    if anchor is None:
        with_key = [i for i in top if _wm_item_command(i)[1] is not None]
        anchor = with_key[-1] if with_key else (top[-1] if top else menu.items[0])
    indent = plist.line_indent(text, anchor.start) if anchor is not menu.items[0] else "  "
    new_item = f"({plist.quote(label)}, SHORTCUT, {plist.quote(key)}, EXEC, {plist.quote(command)})"
    return text[:anchor.end] + f",\n{indent}{new_item}" + text[anchor.end:], []


def _remove_items(text: str, items: list[plist.Node]) -> str:
    """Remove menu items with the comma before them (or after, if first)."""
    spans = []
    for item in items:
        before = text[:item.start]
        m = re.search(r",\s*$", before)
        if m:
            spans.append((m.start(), item.end))
        else:
            m2 = re.compile(r"\s*,[ \t]*\n?").match(text, item.end)
            spans.append((plist.line_start(text, item.start), m2.end() if m2 else item.end))
    return _remove_spans(text, spans)


def _menu_without_ours(node: plist.Node):
    """The menu tree as Python values, our items left out (at any depth)."""
    if node.kind != "array":
        return node.to_py()
    return [_menu_without_ours(i) for i in node.items if not (i.kind == "array" and _wm_is_ours(i))]


def _wm_verify(before: str, after: str, key: str | None) -> None:
    new_menu = _wm_menu(after)
    if _menu_without_ours(_wm_menu(before)) != _menu_without_ours(new_menu):
        raise PlistError("other menu items would change")
    ours = [_wm_item_command(i)[1] for i, _ in _wm_walk(new_menu) if _wm_is_ours(i)]
    if key is None and ours:
        raise PlistError("the item is still there")
    if key is not None and (len(ours) != 1 or ours[0] is None or ours[0].value != key):
        raise PlistError("the item was not written as expected")


def set_wmaker(accel: str | None, label: str = "Read or stop the selected text",
               wmaker_running: bool = False, path: Path | None = None,
               command: str = SPEAK_COMMAND) -> WMResult:
    """Bind ``accel`` (None removes it) through a WMRootMenu item.

    Window Maker re-reads WMRootMenu when it changes, so a running wmaker
    applies it by itself; otherwise it applies at the next X11 login.
    """
    path = path or wmaker_menu_path()
    res = WMResult("windowmaker", ERROR, path)
    key = None
    if accel is not None:
        key = to_wmaker_key(accel)
        res.key = key
        combo = wmaker_combo(key) if key else None
        if not combo or not _Xkb.valid(combo[1]):
            res.detail = f"key not valid for Window Maker: {accel!r}"
            return res
    if not path.is_file():
        res.state = MANUAL
        res.detail = f"{path} does not exist (Window Maker uses the system menu)"
        return res
    with _Lock(_config_home() / "wlmaker" / ".wprefs-sync.json.lock"):
        try:
            original = path.read_text(encoding="utf-8")
            new, conflicts = _wm_edit(original, key, label, command)
        except (OSError, UnicodeDecodeError, PlistError) as e:
            res.state = MANUAL
            res.detail = f"{path} not edited: {e}"
            return res
        if conflicts:
            res.state = CONFLICT
            res.conflicts = conflicts
            res.detail = f"{key} already used: {'; '.join(conflicts)}"
            return res
        if new != original:
            try:
                _wm_verify(original, new, key)
                res.backup = _atomic_replace(path, original, new)
            except (OSError, PlistError) as e:
                res.detail = f"not written: {e}"
                return res
            res.changed = True
    if accel is None:
        res.state = REMOVED
    else:
        res.state = REGISTERED if wmaker_running or not res.changed else PENDING
    res.detail = res.detail or ("written; Window Maker re-reads its menu" if res.changed else "already set")
    logger.info("Window Maker shortcut %s: %s (%s)", key, res.state, res.detail)
    return res


__all__ = [
    "CONFLICT",
    "ERROR",
    "MANUAL",
    "PENDING",
    "REGISTERED",
    "REMOVED",
    "SPEAK_COMMAND",
    "WMResult",
    "parse_gtk_accel",
    "reload_wlmaker",
    "set_wlmaker",
    "set_wmaker",
    "to_wlmaker_key",
    "to_wmaker_key",
    "wlmaker_config_path",
    "wmaker_menu_path",
]
