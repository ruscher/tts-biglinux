"""
Which Wayland protocols the compositor offers (its registry globals).

A tiny client of the Wayland wire protocol — wl_display.get_registry, then
wl_display.sync, reading wl_registry.global events until the callback is
done — so diagnostics and selection capture can tell "no text selected"
from "this compositor has no primary selection" without wayland-info or any
library. One round trip, a few milliseconds; nothing is bound.
"""

from __future__ import annotations

import os
import select
import socket
import struct
import time

from services.session_info import wayland_socket

PRIMARY_SELECTION = "zwp_primary_selection_device_manager_v1"
DATA_CONTROL = ("ext_data_control_manager_v1", "zwlr_data_control_manager_v1")

_DISPLAY_ID, _REGISTRY_ID, _CALLBACK_ID = 1, 2, 3


def _request(obj: int, opcode: int, *args: int) -> bytes:
    size = 8 + 4 * len(args)
    return struct.pack(f"<II{len(args)}I", obj, (size << 16) | opcode, *args)


def list_globals(env: dict | None = None, timeout: float = 1.0) -> dict[str, int] | None:
    """{interface: version} of the compositor, or None if not reachable."""
    path = wayland_socket(env)
    if path is None:
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
    except OSError:
        return None
    globals_: dict[str, int] = {}
    try:
        s.settimeout(timeout)
        s.connect(os.fspath(path))
        s.sendall(_request(_DISPLAY_ID, 1, _REGISTRY_ID) + _request(_DISPLAY_ID, 0, _CALLBACK_ID))
        buf = b""
        deadline = time.monotonic() + timeout
        while True:
            while len(buf) >= 8:
                obj, word = struct.unpack_from("<II", buf)
                size, opcode = word >> 16, word & 0xFFFF
                if size < 8 or len(buf) < size:
                    break
                body, buf = buf[8:size], buf[size:]
                if obj == _CALLBACK_ID and opcode == 0:  # wl_callback.done
                    return globals_
                if obj == _REGISTRY_ID and opcode == 0:  # wl_registry.global
                    (n,) = struct.unpack_from("<I", body, 4)
                    iface = body[8:8 + n].rstrip(b"\0").decode("ascii", "replace")
                    pad = (n + 3) & ~3
                    (version,) = struct.unpack_from("<I", body, 8 + pad)
                    globals_[iface] = version
                elif obj == _DISPLAY_ID and opcode == 0:  # wl_display.error
                    return None
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([s], [], [], left)[0]:
                return None
            chunk = s.recv(65536)
            if not chunk:
                return None
            buf += chunk
    except (OSError, struct.error):
        return None
    finally:
        s.close()


def selection_support(env: dict | None = None) -> dict:
    """What capture can rely on: primary selection and data-control.

    ``reachable`` False: no Wayland compositor answered (X11, tty, tests).
    data-control lets wl-paste read without a window of its own; without it
    wl-clipboard briefly maps a surface to get the focus.
    """
    g = list_globals(env)
    if g is None:
        return {"reachable": False, "primary": None, "data_control": None}
    return {
        "reachable": True,
        "primary": PRIMARY_SELECTION in g,
        "data_control": next((d for d in DATA_CONTROL if d in g), ""),
    }


__all__ = ["DATA_CONTROL", "PRIMARY_SELECTION", "list_globals", "selection_support"]
