#!/usr/bin/env python3
"""Press a key combination through the compositor (virtual-keyboard-v1).

For integration checks in a real Wayland session: the compositor handles
the keys exactly as it handles a physical keyboard's, so its global
shortcuts (wlmaker KeyBindings) fire. Nothing is captured or read; the
compositor must offer zwp_virtual_keyboard_manager_v1 (wlmaker does).

    python3 tests/integration/wl_press_keys.py alt+n
    python3 tests/integration/wl_press_keys.py --layout br --model abnt2 alt+v

The keymap is compiled with libxkbcommon from the given layout (default:
the session's, from wlmaker's Config.plist or "us"). Standard library only.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
import socket
import struct
import sys
import tempfile
import time

# evdev key codes (linux/input-event-codes.h)
KEYS = {
    **{c: code for c, code in zip("qwertyuiop", range(16, 26))},
    **{c: code for c, code in zip("asdfghjkl", range(30, 39))},
    **{c: code for c, code in zip("zxcvbnm", range(44, 51))},
    **{str(n): 1 + n for n in range(1, 10)}, "0": 11,
    "esc": 1, "tab": 15, "space": 57, "return": 28,
    **{f"f{n}": 58 + n for n in range(1, 11)}, "f11": 87, "f12": 88,
}
MODS = {"shift": (42, 1), "ctrl": (29, 4), "control": (29, 4), "alt": (56, 8), "super": (125, 64), "logo": (125, 64)}


class _Names(ctypes.Structure):
    _fields_ = [(n, ctypes.c_char_p) for n in ("rules", "model", "layout", "variant", "options")]


def keymap_text(layout: str, model: str, variant: str = "") -> bytes:
    x = ctypes.CDLL(ctypes.util.find_library("xkbcommon") or "libxkbcommon.so.0")
    x.xkb_context_new.restype = ctypes.c_void_p
    x.xkb_keymap_new_from_names.restype = ctypes.c_void_p
    x.xkb_keymap_new_from_names.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Names), ctypes.c_int]
    x.xkb_keymap_get_as_string.restype = ctypes.c_void_p
    x.xkb_keymap_get_as_string.argtypes = [ctypes.c_void_p, ctypes.c_int]
    ctx = x.xkb_context_new(0)
    names = _Names(b"evdev", model.encode(), layout.encode(), variant.encode(), b"")
    km = x.xkb_keymap_new_from_names(ctx, ctypes.byref(names), 0)
    if not km:
        raise SystemExit(f"cannot compile keymap {layout}/{model}")
    return ctypes.string_at(x.xkb_keymap_get_as_string(km, 1)) + b"\0"


def session_layout() -> tuple[str, str, str]:
    path = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "wlmaker", "Config.plist")
    found = {"Layout": "us", "Model": "pc105", "Variant": ""}
    try:
        for line in open(path, encoding="utf-8"):
            for k in found:
                s = line.strip()
                if s.startswith(f"{k} =") or s.startswith(f"{k}="):
                    found[k] = s.split("=", 1)[1].strip().strip(";").strip().strip('"')
    except OSError:
        pass
    return found["Layout"], found["Model"], found["Variant"]


class Wl:
    def __init__(self) -> None:
        name = os.environ["WAYLAND_DISPLAY"]
        path = name if os.path.isabs(name) else os.path.join(os.environ["XDG_RUNTIME_DIR"], name)
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(path)
        self.s.settimeout(2)
        self.next_id = 2
        self.buf = b""

    def new_id(self) -> int:
        i = self.next_id
        self.next_id += 1
        return i

    def send(self, obj: int, opcode: int, payload: bytes, fds: list[int] = ()) -> None:
        msg = struct.pack("<II", obj, ((8 + len(payload)) << 16) | opcode) + payload
        anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack(f"{len(fds)}i", *fds))] if fds else []
        self.s.sendmsg([msg], anc)

    @staticmethod
    def string(s: str) -> bytes:
        b = s.encode() + b"\0"
        return struct.pack("<I", len(b)) + b + b"\0" * ((4 - len(b) % 4) % 4)

    def events(self):
        while True:
            while len(self.buf) >= 8:
                obj, word = struct.unpack_from("<II", self.buf)
                size = word >> 16
                if len(self.buf) < size:
                    break
                body, self.buf = self.buf[8:size], self.buf[size:]
                yield obj, word & 0xFFFF, body
            chunk = self.s.recv(65536)
            if not chunk:
                raise SystemExit("compositor closed the connection")
            self.buf += chunk

    def roundtrip(self, registry: int | None = None) -> dict[str, tuple[int, int]]:
        cb = self.new_id()
        self.send(1, 0, struct.pack("<I", cb))  # wl_display.sync
        globals_ = {}
        for obj, op, body in self.events():
            if obj == registry and op == 0:
                gname, n = struct.unpack_from("<II", body)
                iface = body[8:8 + n].rstrip(b"\0").decode()
                ver = struct.unpack_from("<I", body, 8 + ((n + 3) & ~3))[0]
                globals_[iface] = (gname, ver)
            elif obj == 1 and op == 0:
                raise SystemExit(f"protocol error: {body!r}")
            elif obj == cb:
                return globals_


def press(combo: str, layout: str, model: str, variant: str, hold_ms: int = 40) -> None:
    parts = combo.lower().split("+")
    mods = [MODS[p] for p in parts[:-1]]
    key = KEYS[parts[-1]]
    wl = Wl()
    registry = wl.new_id()
    wl.send(1, 1, struct.pack("<I", registry))  # get_registry
    g = wl.roundtrip(registry)
    if "zwp_virtual_keyboard_manager_v1" not in g:
        raise SystemExit("the compositor has no zwp_virtual_keyboard_manager_v1")
    seat, mgr, kbd = wl.new_id(), wl.new_id(), wl.new_id()
    for iface, oid, ver in (("wl_seat", seat, 1), ("zwp_virtual_keyboard_manager_v1", mgr, 1)):
        wl.send(registry, 0, struct.pack("<I", g[iface][0]) + wl.string(iface) + struct.pack("<II", ver, oid))
    wl.send(mgr, 0, struct.pack("<II", seat, kbd))  # create_virtual_keyboard
    km = keymap_text(layout, model, variant)
    with tempfile.TemporaryFile() as f:
        f.write(km)
        f.flush()
        wl.send(kbd, 0, struct.pack("<II", 1, len(km)), [f.fileno()])  # keymap(xkb_v1, fd, size)
        wl.roundtrip()
    t = int(time.monotonic() * 1000) & 0xFFFFFFFF
    mask = 0
    for code, bit in mods:
        wl.send(kbd, 1, struct.pack("<III", t, code, 1))
        mask |= bit
        wl.send(kbd, 2, struct.pack("<IIII", mask, 0, 0, 0))
    wl.send(kbd, 1, struct.pack("<III", t, key, 1))
    wl.roundtrip()
    time.sleep(hold_ms / 1000)
    wl.send(kbd, 1, struct.pack("<III", t + hold_ms, key, 0))
    for code, bit in reversed(mods):
        wl.send(kbd, 1, struct.pack("<III", t + hold_ms, code, 0))
        mask &= ~bit
        wl.send(kbd, 2, struct.pack("<IIII", mask, 0, 0, 0))
    wl.send(kbd, 3, b"")  # destroy
    wl.roundtrip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("combo", help="e.g. alt+v, ctrl+alt+t")
    lay, mod, var = session_layout()
    ap.add_argument("--layout", default=lay)
    ap.add_argument("--model", default=mod)
    ap.add_argument("--variant", default=var)
    a = ap.parse_args()
    press(a.combo, a.layout, a.model, a.variant)
    return 0


if __name__ == "__main__":
    sys.exit(main())
