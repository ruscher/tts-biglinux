"""
A few X11 facts for selection capture, read with libxcb through ctypes.

Used inside Wayland sessions that run XWayland: wlroots only bridges the
selections between X11 and Wayland when the compositor gives XWayland a seat
(``wlr_xwayland_set_seat``). wlmaker does not (yet), so text selected in an
X11 application is only in the X server's PRIMARY. The window that has the
focus tells which side the user selected on:

* ``_NET_ACTIVE_WINDOW`` is set by the compositor's X window manager (xwm) to
  the focused X11 window, and to one of its own windows when a Wayland window
  has the focus. Windows of one X client share the bits above the server's
  resource-id mask, so "belongs to the xwm" is "same client as the owner of
  ``WM_S0``" (the selection a window manager owns).

No new dependency: libxcb is present wherever an X server or XWayland is.
Every function returns None when X is not reachable. Callers must not use
this when XWayland is not running yet — connecting would start a lazy one.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class _Cookie(ctypes.Structure):
    _fields_ = [("sequence", ctypes.c_uint)]


class _ScreenIter(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p), ("rem", ctypes.c_int), ("index", ctypes.c_int)]


_xcb = None
_libc = None


def _lib():
    """libxcb with the prototypes used here (None if missing)."""
    global _xcb, _libc
    if _xcb is not None:
        return _xcb or None
    name = ctypes.util.find_library("xcb")
    if not name:
        _xcb = False
        return None
    try:
        x = ctypes.CDLL(name)
        _libc = ctypes.CDLL(ctypes.util.find_library("c"))
    except OSError:
        _xcb = False
        return None
    vp, u32, u8, u16 = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint8, ctypes.c_uint16
    x.xcb_connect.restype = vp
    x.xcb_connect.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
    x.xcb_connection_has_error.argtypes = [vp]
    x.xcb_disconnect.argtypes = [vp]
    x.xcb_get_setup.restype = vp
    x.xcb_get_setup.argtypes = [vp]
    x.xcb_setup_roots_iterator.restype = _ScreenIter
    x.xcb_setup_roots_iterator.argtypes = [vp]
    x.xcb_screen_next.argtypes = [ctypes.POINTER(_ScreenIter)]
    x.xcb_intern_atom.restype = _Cookie
    x.xcb_intern_atom.argtypes = [vp, u8, u16, ctypes.c_char_p]
    x.xcb_intern_atom_reply.restype = vp
    x.xcb_intern_atom_reply.argtypes = [vp, _Cookie, vp]
    x.xcb_get_selection_owner.restype = _Cookie
    x.xcb_get_selection_owner.argtypes = [vp, u32]
    x.xcb_get_selection_owner_reply.restype = vp
    x.xcb_get_selection_owner_reply.argtypes = [vp, _Cookie, vp]
    x.xcb_get_property.restype = _Cookie
    x.xcb_get_property.argtypes = [vp, u8, u32, u32, u32, u32, u32]
    x.xcb_get_property_reply.restype = vp
    x.xcb_get_property_reply.argtypes = [vp, _Cookie, vp]
    x.xcb_get_property_value.restype = vp
    x.xcb_get_property_value.argtypes = [vp]
    x.xcb_get_property_value_length.restype = ctypes.c_int
    x.xcb_get_property_value_length.argtypes = [vp]
    _libc.free.argtypes = [vp]
    _xcb = x
    return x


def _u32(ptr: int, offset: int) -> int:
    return ctypes.c_uint32.from_address(ptr + offset).value


XCB_ATOM_PRIMARY = 1
XCB_ATOM_WINDOW = 33


@dataclass(frozen=True)
class X11Focus:
    active_window: int  # 0: none
    active_is_x11_client: bool  # an X11 application (not the compositor's xwm) has the focus
    primary_owner: int  # 0: nobody owns PRIMARY
    primary_is_x11_client: bool  # owned by an X11 application (not the xwm)


class _Conn:
    def __init__(self, display: str | None) -> None:
        self.x = _lib()
        if self.x is None:
            raise OSError("libxcb not found")
        screen = ctypes.c_int(0)
        self.c = self.x.xcb_connect(display.encode() if display else None, ctypes.byref(screen))
        if not self.c or self.x.xcb_connection_has_error(self.c):
            if self.c:
                self.x.xcb_disconnect(self.c)
            raise OSError("cannot connect to the X server")
        self.screen = screen.value
        setup = self.x.xcb_get_setup(self.c)
        # xcb_setup_t: status, pad, major, minor, length, release, base, mask
        self.mask = _u32(setup, 16)
        it = self.x.xcb_setup_roots_iterator(setup)
        for _ in range(self.screen):
            self.x.xcb_screen_next(ctypes.byref(it))
        self.root = _u32(it.data, 0)  # xcb_screen_t.root

    def close(self) -> None:
        self.x.xcb_disconnect(self.c)

    def _reply(self, ptr) -> int:
        if not ptr:
            raise OSError("no reply from the X server")
        return ptr

    def atom(self, name: str) -> int:
        raw = name.encode()
        r = self._reply(self.x.xcb_intern_atom_reply(self.c, self.x.xcb_intern_atom(self.c, 0, len(raw), raw), None))
        try:
            return _u32(r, 8)
        finally:
            _libc.free(r)

    def selection_owner(self, atom: int) -> int:
        r = self._reply(self.x.xcb_get_selection_owner_reply(self.c, self.x.xcb_get_selection_owner(self.c, atom), None))
        try:
            return _u32(r, 8)
        finally:
            _libc.free(r)

    def window_property(self, window: int, prop: int) -> int:
        r = self._reply(self.x.xcb_get_property_reply(
            self.c, self.x.xcb_get_property(self.c, 0, window, prop, XCB_ATOM_WINDOW, 0, 1), None))
        try:
            if self.x.xcb_get_property_value_length(r) < 4:
                return 0
            return _u32(self.x.xcb_get_property_value(r), 0)
        finally:
            _libc.free(r)

    def same_client(self, a: int, b: int) -> bool:
        return bool(a and b) and (a & ~self.mask) == (b & ~self.mask)


def focus(display: str | None = None) -> X11Focus | None:
    """Focus and PRIMARY owner on ``display``; None if X is not reachable."""
    try:
        conn = _Conn(display)
    except OSError as e:
        logger.debug("X11 not reachable: %s", e)
        return None
    try:
        wm = conn.selection_owner(conn.atom(f"WM_S{conn.screen}"))
        active = conn.window_property(conn.root, conn.atom("_NET_ACTIVE_WINDOW"))
        owner = conn.selection_owner(XCB_ATOM_PRIMARY)
        return X11Focus(
            active_window=active,
            active_is_x11_client=bool(active) and not conn.same_client(active, wm),
            primary_owner=owner,
            primary_is_x11_client=bool(owner) and not conn.same_client(owner, wm),
        )
    except OSError as e:
        logger.debug("X11 query failed: %s", e)
        return None
    finally:
        conn.close()


__all__ = ["X11Focus", "focus"]
