"""
"Desktop integration" — is the whole chain working on this desktop?

One small group in Advanced options: which desktop was recognized, the
shortcut as the desktop confirmed it (and a "Fix integration" button when it
is not active), how selected text is captured, whether the tray icon is
really shown, the "read copied text" option, and a guided test that walks
through the real chain: selection → shortcut → capture → speech.

Facts come from the application (what actually happened) and from
services.session_info / wayland_globals (read off the main thread). The
selected or copied text itself is never shown — only its length and origin.
"""

from __future__ import annotations

import logging
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from services import shortcut_service  # noqa: E402
from ui.components import create_action_row_with_switch, create_state_pill, set_row_icon, set_state_pill  # noqa: E402
from utils.i18n import _  # noqa: E402

logger = logging.getLogger(__name__)


def desktop_label(desktop: str, server: str) -> str:
    names = {
        "wlmaker": "Big WindowMaker (Wayland)",
        "windowmaker": "Big WindowMaker (X11)",
        "kde": "KDE Plasma",
        "gnome": "GNOME",
        "xfce": "Xfce",
        "cinnamon": "Cinnamon",
    }
    name = names.get(desktop) or _("Desktop not recognized")
    if desktop in ("kde", "gnome", "unknown") and server in ("wayland", "x11"):
        name += f" ({'Wayland' if server == 'wayland' else 'X11'})"
    return name


def source_label(source: str) -> str:
    return {
        "wayland-primary": _("selection (Wayland)"),
        "xwayland-primary": _("selection of an X11 program (XWayland)"),
        "x11-primary": _("selection (X11)"),
        "clipboard": _("copied text"),
    }.get(source, source)


def capture_label(last: str) -> str:
    """``"44 chars from wayland-primary"`` → a sentence without the text."""
    if not last:
        return ""
    if " chars from " in last:
        n, src = last.split(" chars from ", 1)
        return _("Last reading: {count} characters from the {source}").format(count=n, source=source_label(src))
    if last == "no-text-selected":
        return _("Last time nothing was selected")
    return _("Last time the text could not be read")


class IntegrationGroup(Adw.PreferencesGroup):
    """The "Desktop integration" group (lives in the Advanced dialog)."""

    def __init__(self, app, settings_service) -> None:
        super().__init__()
        self._app = app
        self._settings_service = settings_service
        self._session = None
        self._support: dict = {}
        self.set_title(_("Desktop integration"))
        self.set_description(_("Check that the shortcut, the text capture and the tray icon work on this desktop."))

        self._desktop_row = Adw.ActionRow(title=_("Desktop"))
        set_row_icon(self._desktop_row, "computer-symbolic")
        self.add(self._desktop_row)

        self._shortcut_row = Adw.ActionRow(title=_("Global shortcut"))
        set_row_icon(self._shortcut_row, "input-keyboard-symbolic")
        self._shortcut_pill = create_state_pill(_("Checking…"))
        self._fix_button = Gtk.Button(label=_("Fix integration"))
        self._fix_button.set_valign(Gtk.Align.CENTER)
        self._fix_button.add_css_class("flat")
        self._fix_button.connect("clicked", self._on_fix)
        self._shortcut_row.add_suffix(self._fix_button)
        self._shortcut_row.add_suffix(self._shortcut_pill)
        self.add(self._shortcut_row)

        self._capture_row = Adw.ActionRow(title=_("Selected text"))
        set_row_icon(self._capture_row, "edit-select-all-symbolic")
        self.add(self._capture_row)

        settings = settings_service.get()
        clip_row, self._clip_switch = create_action_row_with_switch(
            title=_("Read copied text"),
            subtitle=_("When nothing is selected, read what was copied (Ctrl+C). Off: an old copied text is never read by mistake."),
            active=settings.shortcut.clipboard_fallback,
            on_toggled=self._on_clipboard_fallback,
            accessible_name=_("Read copied text when nothing is selected"),
        )
        set_row_icon(clip_row, "edit-paste-symbolic")
        self.add(clip_row)

        self._tray_row = Adw.ActionRow(title=_("Tray icon"))
        set_row_icon(self._tray_row, "tts-biglinux-symbolic")
        self._tray_pill = create_state_pill("")
        self._tray_row.add_suffix(self._tray_pill)
        self.add(self._tray_row)

        test_row = Adw.ActionRow(title=_("Test the shortcut"))
        test_row.set_subtitle(_("Select a sample text, press the shortcut and listen"))
        set_row_icon(test_row, "media-playback-start-symbolic")
        test_button = Gtk.Button(label=_("Start test"))
        test_button.set_valign(Gtk.Align.CENTER)
        test_button.add_css_class("suggested-action")
        test_button.connect("clicked", self._on_test)
        test_row.add_suffix(test_button)
        test_row.set_activatable_widget(test_button)
        self.add(test_row)

        app.add_shortcut_listener(lambda _s: self.refresh())
        app.add_integration_listener(self.refresh)
        self._probe()
        self.refresh()

    # ── facts ──

    def _probe(self) -> None:
        """Session and protocol facts, read off the main thread."""

        def work() -> None:
            from services import session_info
            from services.wayland_globals import selection_support

            info = session_info.detect()
            support = selection_support() if info.display_server == "wayland" else {}
            GLib.idle_add(self._on_probed, info, support)

        threading.Thread(target=work, daemon=True).start()

    def _on_probed(self, info, support) -> bool:
        self._session, self._support = info, support
        self.refresh()
        return False

    def refresh(self) -> None:
        app = self._app
        info = self._session
        if info is not None:
            self._desktop_row.set_subtitle(desktop_label(info.desktop, info.display_server))

        st = app.shortcut_status
        keys = shortcut_service.display_text(st.accel) if st.accel else ""
        if st.registered is None:
            sub, pill, kind = _("Checking the shortcut with the desktop…"), _("Checking…"), ""
        elif st.registered is False:
            sub, pill, kind = st.message or _("The shortcut is not active."), _("Not active"), "error"
        elif st.pending:
            sub, pill, kind = st.message, _("After login"), "warning"
        else:
            sub = _("{keys} reads or stops the selected text").format(keys=keys)
            pill, kind = _("Active"), "ready"
            if app.last_speak_request:
                when = time.strftime("%H:%M", time.localtime(app.last_speak_request))
                sub += " · " + _("last used at {time}").format(time=when)
                pill = _("Working")
        self._shortcut_row.set_subtitle(GLib.markup_escape_text(sub))
        self._shortcut_row.set_tooltip_text(st.mechanism or None)
        set_state_pill(self._shortcut_pill, pill, kind)
        self._fix_button.set_visible(st.registered is False or st.pending)

        if info is not None:
            if info.display_server == "wayland":
                if self._support.get("primary") is False:
                    cap = _("This desktop does not share selected text. Turn on “Read copied text” below.")
                elif info.xwayland or info.desktop == "wlmaker":
                    cap = _("Read from the selection, also in X11 programs (XWayland)")
                else:
                    cap = _("Read from the selection, no copying needed")
            else:
                cap = _("Read from the selection, no copying needed")
            last = capture_label(app.last_capture)
            self._capture_row.set_subtitle(GLib.markup_escape_text(cap + (f"\n{last}" if last else "")))

        state, detail = app.tray_state, app.tray_detail
        if state == "visible":
            text, kind, sub = _("Shown"), "ready", _("In the system tray; click it to read or stop")
        elif state == "starting":
            text, kind, sub = _("Starting…"), "busy", _("Waiting for the system tray")
        elif state == "unavailable":
            text, kind = _("Unavailable"), "warning"
            sub = (_("No system tray on this desktop. Reading works without it.")
                   if "No system tray" in detail else
                   _("The tray icon could not be shown. Reading works without it."))
        else:
            text, kind, sub = _("Off"), "", _("Turn it on in the options above")
        set_state_pill(self._tray_pill, text, kind)
        self._tray_row.set_subtitle(sub)

    # ── actions ──

    def _on_fix(self, _button) -> None:
        self._app.apply_shortcut(self._settings_service.get().shortcut.keybinding)

    def _on_clipboard_fallback(self, active: bool) -> None:
        settings = self._settings_service.get()
        settings.shortcut.clipboard_fallback = active
        self._settings_service.save(settings)

    def _on_test(self, _button) -> None:
        ShortcutTestDialog(self._app).present(self.get_root())


class ShortcutTestDialog(Adw.Dialog):
    """Guided test of the real chain, one step lighting up at a time."""

    SAMPLE = None  # set in __init__ (translated)
    TIMEOUT_S = 25

    def __init__(self, app) -> None:
        super().__init__()
        self._app = app
        self._started = time.time()
        self._poll_id = 0
        self._heard_asked = False
        self.set_title(_("Test the shortcut"))
        self.set_content_width(460)

        accel = app.settings.shortcut.keybinding
        self._keys = shortcut_service.display_text(accel)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)

        self._entry = Gtk.Entry()
        self._entry.set_text(_("BigLinux TTS reads the text you select, with accents and numbers: 2026."))
        self._entry.update_property([Gtk.AccessibleProperty.LABEL], [_("Sample text")])
        box.append(self._entry)

        steps = Gtk.ListBox()
        steps.add_css_class("boxed-list")
        steps.set_selection_mode(Gtk.SelectionMode.NONE)
        self._steps = {}
        for key, title in (
            ("select", _("The sample text above is selected")),
            ("press", _("Press {keys} now").format(keys=self._keys)),
            ("capture", _("BigLinux TTS captures the selection")),
            ("speak", _("The text is read aloud")),
        ):
            row = Adw.ActionRow(title=title)
            icon = Gtk.Image.new_from_icon_name("content-loading-symbolic")
            icon.add_css_class("dim-label")
            row.add_prefix(icon)
            steps.append(row)
            self._steps[key] = (row, icon)
        box.append(steps)

        self._result = Gtk.Label()
        self._result.set_wrap(True)
        self._result.set_xalign(0)
        box.append(self._result)

        self._heard_box = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        no = Gtk.Button(label=_("I heard nothing"))
        no.connect("clicked", lambda *_a: self._finish(False))
        yes = Gtk.Button(label=_("I heard it"))
        yes.add_css_class("suggested-action")
        yes.connect("clicked", lambda *_a: self._finish(True))
        self._heard_box.append(no)
        self._heard_box.append(yes)
        self._heard_box.set_visible(False)
        box.append(self._heard_box)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(box)
        self.set_child(toolbar)

        self.connect("map", self._on_map)
        self.connect("closed", self._on_closed)

    def _mark(self, key: str, ok: bool, subtitle: str = "") -> None:
        row, icon = self._steps[key]
        icon.set_from_icon_name("object-select-symbolic" if ok else "dialog-warning-symbolic")
        icon.remove_css_class("dim-label")
        icon.add_css_class("success" if ok else "warning")
        if subtitle:
            row.set_subtitle(GLib.markup_escape_text(subtitle))

    def _on_map(self, *_a) -> None:
        # Selecting the text makes this window the owner of the selection —
        # exactly what selecting with the mouse in any other program does.
        self._entry.grab_focus()
        self._entry.select_region(0, -1)
        self._mark("select", True)
        self._started = time.time()
        self._poll_id = GLib.timeout_add(200, self._poll)

    def _on_closed(self, *_a) -> None:
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0

    def _poll(self) -> bool:
        from config import TTSState

        app = self._app
        pressed = app.last_speak_request >= self._started
        if pressed:
            self._mark("press", True)
            if app.last_capture:
                ok = " chars from " in app.last_capture
                self._mark("capture", ok, capture_label(app.last_capture))
                tts = app._tts_service
                if ok and tts is not None and tts.state == TTSState.SPEAKING and not self._heard_asked:
                    self._mark("speak", True)
                    self._heard_asked = True
                    self._result.set_label(_("Did you hear the sample text?"))
                    self._heard_box.set_visible(True)
                elif not ok:
                    self._result.set_label(_("The text was not captured. Keep the sample text selected and try again."))
            if not self._heard_asked:
                return True
            self._poll_id = 0
            return False
        if time.time() - self._started > self.TIMEOUT_S:
            self._mark("press", False)
            self._result.set_label(_(
                "The shortcut did not reach BigLinux TTS. Use “Fix integration”; "
                "if the shortcut waits for a new login, log out and in again."))
            self._poll_id = 0
            return False
        return True

    def _finish(self, heard: bool) -> None:
        self._heard_box.set_visible(False)
        if heard:
            self._result.set_label(_("Everything works: select text anywhere and press {keys}.").format(keys=self._keys))
        else:
            self._mark("speak", False)
            self._result.set_label(_(
                "The text was read, but no sound reached you. Check the volume and the audio output; "
                "“biglinux-tts --diagnose” in a terminal shows the details."))


__all__ = ["IntegrationGroup", "ShortcutTestDialog", "capture_label", "desktop_label"]
