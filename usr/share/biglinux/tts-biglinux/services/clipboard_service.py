"""
Clipboard service — capture the selected text on Wayland or X11.

The shortcut reads the *selection* (PRIMARY): what is highlighted now, not
what was copied with Ctrl+C some time ago. The clipboard is read only when
the user turned that on (``clipboard_fallback``), and the result says which
one the text came from.

Where the selection lives:

* X11: the X server's PRIMARY (xsel, or xclip).
* Wayland: the compositor's primary selection (wl-paste --primary, through
  the data-control protocol, so no window and no focus change).
* Wayland, X11 application (XWayland) focused: wlroots copies selections
  between X11 and Wayland only when the compositor gives XWayland a seat;
  wlmaker does not, so the text is only in XWayland's PRIMARY. When an X11
  window has the focus it is read there first (see x11_selection).

Nothing here changes the clipboard or the selection, and the captured text
is never logged — only its length and where it came from.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import NamedTuple

logger = logging.getLogger(__name__)

# Reasons (ClipboardResult.error) the application turns into messages.
NO_SELECTION = "no-text-selected"
PRIMARY_UNSUPPORTED = "primary-unsupported"
WAYLAND_TOOL_MISSING = "wl-clipboard not installed"
X11_TOOL_MISSING = "xsel or xclip not installed"
TIMEOUT = "timeout"

# Where the text came from (ClipboardResult.source).
SRC_WAYLAND_PRIMARY = "wayland-primary"
SRC_XWAYLAND_PRIMARY = "xwayland-primary"
SRC_X11_PRIMARY = "x11-primary"
SRC_CLIPBOARD = "clipboard"


class ClipboardResult(NamedTuple):
    """Result of a capture attempt (``text`` is never logged)."""

    text: str
    success: bool
    error: str
    source: str = ""


def is_wayland() -> bool:
    """Running on Wayland (a Wayland socket is set, not only DISPLAY)."""
    return (
        os.environ.get("XDG_SESSION_TYPE") == "wayland"
        or os.environ.get("WAYLAND_DISPLAY") is not None
    )


def get_selected_text(max_chars: int = 0, *, clipboard_fallback: bool = False) -> ClipboardResult:
    """
    Capture the currently selected (primary selection) text.

    Args:
        max_chars: Maximum characters to return (0 = all; the reading limit
            is applied by TTSService.speak).
        clipboard_fallback: Read the clipboard (Ctrl+C) when nothing is
            selected. Off by default: an old copied text must not be read
            as if it were the selection.

    Returns:
        ClipboardResult with text, success flag, reason and source.
    """
    if is_wayland():
        return _get_text_wayland(max_chars, clipboard_fallback)
    return _get_text_x11(max_chars, clipboard_fallback)


# ── Wayland ──────────────────────────────────────────────────────────


def _xwayland_focus():
    """X11Focus when an XWayland server already runs, else None.

    Never connects to a lazy XWayland that has not started: that would
    start it just to find out nobody uses it.
    """
    display = os.environ.get("DISPLAY")
    if not display:
        return None
    from services import session_info, x11_selection

    if not session_info.xwayland_running():
        return None
    return x11_selection.focus(display)


def _primary_supported() -> bool | None:
    """Does the compositor offer the primary selection? None: unknown."""
    from services.wayland_globals import selection_support

    return selection_support().get("primary")


def _get_text_wayland(max_chars: int, clipboard_fallback: bool = False) -> ClipboardResult:
    """Capture text on Wayland: XWayland's PRIMARY when an X11 app is
    focused, the Wayland primary selection otherwise."""
    focus = None
    try:
        focus = _xwayland_focus()
    except Exception as e:  # never let X trouble stop a Wayland capture
        logger.debug("XWayland focus check failed: %s", e)
    if focus is not None and focus.active_is_x11_client and focus.primary_owner:
        result = _x11_primary(max_chars, source=SRC_XWAYLAND_PRIMARY)
        if result.success:
            return result

    if not shutil.which("wl-paste"):
        return ClipboardResult("", False, WAYLAND_TOOL_MISSING)

    # "--type text" asks for a text representation only: an image or a file
    # in the selection is skipped instead of being read as garbage.
    result = _run_capture(["wl-paste", "--primary", "--no-newline", "--type", "text"], max_chars)
    if result.success:
        return result._replace(source=SRC_WAYLAND_PRIMARY)
    if result.error == TIMEOUT:
        return result

    if clipboard_fallback:
        clip = _run_capture(["wl-paste", "--no-newline", "--type", "text"], max_chars)
        if clip.success:
            return clip._replace(source=SRC_CLIPBOARD)

    if _primary_supported() is False:
        return ClipboardResult("", False, PRIMARY_UNSUPPORTED)
    return ClipboardResult("", False, NO_SELECTION)


# ── X11 ──────────────────────────────────────────────────────────────


def _x11_commands(selection: str) -> list[list[str]]:
    cmds = []
    if shutil.which("xsel"):
        cmds.append(["xsel", f"--{selection}", "-o"])
    if shutil.which("xclip"):
        cmds.append(["xclip", "-selection", selection, "-o"])
    return cmds


def _x11_primary(max_chars: int, source: str = SRC_X11_PRIMARY) -> ClipboardResult:
    cmds = _x11_commands("primary")
    if not cmds:
        return ClipboardResult("", False, X11_TOOL_MISSING)
    last = ClipboardResult("", False, NO_SELECTION)
    for args in cmds:
        last = _run_capture(args, max_chars)
        if last.success:
            return last._replace(source=source)
    return last


def _get_text_x11(max_chars: int, clipboard_fallback: bool = False) -> ClipboardResult:
    """Capture text on X11 via xsel or xclip (PRIMARY first)."""
    if not _x11_commands("primary"):
        return ClipboardResult("", False, X11_TOOL_MISSING)
    result = _x11_primary(max_chars)
    if result.success or result.error == TIMEOUT:
        return result
    if clipboard_fallback:
        for args in _x11_commands("clipboard"):
            clip = _run_capture(args, max_chars)
            if clip.success:
                return clip._replace(source=SRC_CLIPBOARD)
    return ClipboardResult("", False, NO_SELECTION)


def _run_capture(args: list[str], max_chars: int) -> ClipboardResult:
    """Run a clipboard capture command safely."""
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=3,
        )
        if proc.returncode == 0:
            # Decode leniently: one invalid byte must not lose the whole text
            # (a decode exception used to kill the capture thread silently).
            text = proc.stdout.decode("utf-8", errors="replace").replace("\x00", "").strip()
            if text:
                if max_chars > 0 and len(text) > max_chars:
                    text = text[:max_chars]
                    logger.info("Text truncated to %d chars", max_chars)
                return ClipboardResult(text, True, "")
        return ClipboardResult("", False, "empty")
    except subprocess.TimeoutExpired:
        logger.warning("Clipboard command timed out: %s", args[0])
        return ClipboardResult("", False, TIMEOUT)
    except OSError as e:
        logger.warning("Clipboard command failed: %s", e)
        return ClipboardResult("", False, str(e))


def describe(result: ClipboardResult) -> str:
    """Where the text came from / why there is none — no text, for logs
    and diagnostics."""
    if result.success:
        return f"{len(result.text)} chars from {result.source or 'selection'}"
    return result.error or "nothing"
