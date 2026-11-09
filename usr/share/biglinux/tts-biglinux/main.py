"""Entry point for BigLinux TTS application."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from config import APP_VERSION
from utils.i18n import _

# The native engine (tts_engine.so) is architecture-dependent, so the package
# installs it in /usr/lib. Appended: a source checkout's own build wins.
NATIVE_ENGINE_DIR = "/usr/lib/tts-biglinux"
if os.path.isdir(NATIVE_ENGINE_DIR):
    sys.path.append(NATIVE_ENGINE_DIR)


def main() -> None:
    """Application entry point with CLI argument parsing."""
    parser = argparse.ArgumentParser(
        prog="biglinux-tts",
        description=_("BigLinux Text-to-Speech — Read selected text aloud"),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=_("Enable debug logging"),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {APP_VERSION}",
    )

    parser.add_argument(
        "--diagnose",
        action="store_true",
        help=_("Show how the shortcut, text capture, tray icon, voices and audio are set up, then exit"),
    )
    parser.add_argument(
        "--repair-shortcut",
        action="store_true",
        help=_("Register the global shortcut again on this desktop, then exit"),
    )
    parser.add_argument(
        "--remove-shortcut",
        action="store_true",
        help=_("Remove the shortcut this app added to Big WindowMaker, then exit"),
    )

    args, unknown = parser.parse_known_args()

    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.diagnose or args.repair_shortcut or args.remove_shortcut:
        sys.exit(_maintenance(args))

    logger = logging.getLogger(__name__)
    logger.debug("Starting BigLinux TTS")

    # Import GTK after logging is configured
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk

    # Plasma writes gtk-application-prefer-dark-theme=true to GTK's
    # settings.ini; libadwaita warns about it at every start. The dark style
    # still follows the desktop (AdwStyleManager reads the portal).
    if Gtk.init_check():
        gtk_settings = Gtk.Settings.get_default()
        if gtk_settings is not None and gtk_settings.props.gtk_application_prefer_dark_theme:
            gtk_settings.props.gtk_application_prefer_dark_theme = False

    from application import TTSApplication

    app = TTSApplication()
    
    # Pass the program name and any unparsed arguments to GTK GApplication
    gtk_args = [sys.argv[0]] + unknown
    sys.exit(app.run(gtk_args))


def _maintenance(args) -> int:
    """--diagnose / --repair-shortcut / --remove-shortcut: no window, no
    GApplication (a running app is left alone), and never any selected or
    copied text in the output."""
    from services.settings_service import SettingsService

    settings = SettingsService().get()
    if args.diagnose:
        from services.diagnostics import (
            collect_diagnostics,
            collect_integration,
            format_diagnostics,
            format_integration,
        )

        print(format_diagnostics(collect_diagnostics(settings)))
        print()
        data = collect_integration(settings)
        print(format_integration(data))
        return 1 if data["problems"] else 0

    from services import shortcut_service

    if args.remove_shortcut:
        status = 0
        for res in shortcut_service.unregister_bigwm():
            print(f"{res.backend}: {res.state}  {res.detail or ''}  [{res.path}]")
            status |= res.state not in ("removed", "manual")
        return status

    accel = settings.shortcut.keybinding
    st = shortcut_service.register(accel)
    state = "registered" if st.registered else ("unknown desktop" if st.registered is None else "NOT registered")
    print(f"{shortcut_service.display_text(accel)} ({st.desktop or '-'}): {state}{' (after the next login)' if st.pending else ''}")
    if st.message:
        print(st.message)
    if st.mechanism:
        print(st.mechanism)
    for note in st.notes:
        print(f"also: {note}")
    return 0 if st.registered else 1


if __name__ == "__main__":
    main()
