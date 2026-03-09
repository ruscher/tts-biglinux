"""
Voice Manager Dialog — Install / Remove TTS voice packages.

Presents a modern Adwaita interface for browsing, installing, and removing
voice packages for all TTS engines (RHVoice, Piper, espeak-ng) via pacman.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from utils.i18n import _

logger = logging.getLogger(__name__)

# ── Language / Region display names ──────────────────────────────────

_LANG_DISPLAY: dict[str, str] = {
    # RHVoice languages
    "albanian": "🇦🇱  Albanian",
    "brazilian-portuguese": "🇧🇷  Brazilian Portuguese",
    "croatian": "🇭🇷  Croatian",
    "czech": "🇨🇿  Czech",
    "english": "🇬🇧  English",
    "esperanto": "🌍  Esperanto",
    "georgian": "🇬🇪  Georgian",
    "kyrgyz": "🇰🇬  Kyrgyz",
    "macedonian": "🇲🇰  Macedonian",
    "polish": "🇵🇱  Polish",
    "russian": "🇷🇺  Russian",
    "serbian": "🇷🇸  Serbian",
    "slovak": "🇸🇰  Slovak",
    "spanish": "🇪🇸  Spanish",
    "tatar": "Tatar",
    "ukrainian": "🇺🇦  Ukrainian",
    "uzbek": "🇺🇿  Uzbek",
    # Piper locale codes → display
    "ar-jo": "🇯🇴  Arabic",
    "ca-es": "🚩  Catalan",
    "cs-cz": "🇨🇿  Czech",
    "cy-gb": "🏴  Welsh",
    "da-dk": "🇩🇰  Danish",
    "de-de": "🇩🇪  German",
    "el-gr": "🇬🇷  Greek",
    "en-gb": "🇬🇧  English (UK)",
    "en-us": "🇺🇸  English (US)",
    "es-es": "🇪🇸  Spanish (Spain)",
    "es-mx": "🇲🇽  Spanish (Mexico)",
    "fa-ir": "🇮🇷  Persian",
    "fi-fi": "🇫🇮  Finnish",
    "fr-fr": "🇫🇷  French",
    "hu-hu": "🇭🇺  Hungarian",
    "is-is": "🇮🇸  Icelandic",
    "it-it": "🇮🇹  Italian",
    "ka-ge": "🇬🇪  Georgian",
    "kk-kz": "🇰🇿  Kazakh",
    "lb-lu": "🇱🇺  Luxembourgish",
    "ne-np": "🇳🇵  Nepali",
    "nl-be": "🇧🇪  Dutch (Belgium)",
    "nl-nl": "🇳🇱  Dutch",
    "no-no": "🇳🇴  Norwegian",
    "pl-pl": "🇵🇱  Polish",
    "pt-br": "🇧🇷  Portuguese (Brazil)",
    "pt-pt": "🇵🇹  Portuguese (Portugal)",
    "ro-ro": "🇷🇴  Romanian",
    "ru-ru": "🇷🇺  Russian",
    "sk-sk": "🇸🇰  Slovak",
    "sl-si": "🇸🇮  Slovenian",
    "sr-rs": "🇷🇸  Serbian",
    "sv-se": "🇸🇪  Swedish",
    "sw-cd": "🇨🇩  Swahili",
    "tr-tr": "🇹🇷  Turkish",
    "uk-ua": "🇺🇦  Ukrainian",
    "vi-vn": "🇻🇳  Vietnamese",
    "zh-cn": "🇨🇳  Chinese",
}

_GENDER_ICON: dict[str, str] = {
    "female": "♀",
    "male": "♂",
}


# ── Data helpers ─────────────────────────────────────────────────────


def _query_packages(search_term: str, prefix: str) -> list[dict[str, str]]:
    """Query pacman for packages matching a search term.

    Returns a list of dicts with keys:
        pkg, display_name, language, version, installed, description, engine
    """
    packages: list[dict[str, str]] = []
    try:
        proc = subprocess.run(
            ["pacman", "-Ss", search_term],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return packages

        lines = proc.stdout.strip().splitlines()
        i = 0
        while i < len(lines):
            header = lines[i].strip()
            desc = lines[i + 1].strip() if i + 1 < len(lines) else ""
            i += 2

            # Parse: repo/pkg-name version (group) [installed]
            m = re.match(
                r"^(?:\S+/)?(\S+)\s+(\S+)(?:\s+\(.*?\))?(?:\s+\[.*?\])?\s*$",
                header,
            )
            if not m:
                continue
            pkg_name = m.group(1)
            version = m.group(2)
            installed = "[instalado]" in header or "[installed]" in header

            if not pkg_name.startswith(prefix):
                continue

            packages.append(
                {
                    "pkg": pkg_name,
                    "version": version,
                    "installed": "yes" if installed else "no",
                    "description": desc,
                }
            )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.error("Failed to query pacman for %s: %s", search_term, e)
    return packages


def _query_all_voice_packages() -> dict[str, list[dict[str, str]]]:
    """Query pacman for all voice packages across all engines.

    Returns dict keyed by engine name: {"RHVoice": [...], "Piper": [...], "espeak-ng": [...]}
    """
    result: dict[str, list[dict[str, str]]] = {}

    # ── RHVoice voices ──
    rhvoice_pkgs = _query_packages("rhvoice-voice-", "rhvoice-voice-")
    for pkg in rhvoice_pkgs:
        voice_name = pkg["pkg"].removeprefix("rhvoice-voice-")
        lang_match = re.search(r"for\s+(\S+(?:-\S+)?)\s+language", pkg["description"], re.I)
        language = lang_match.group(1).lower() if lang_match else "unknown"

        pkg["voice_name"] = voice_name
        pkg["display_name"] = voice_name.replace("-", " ").title()
        pkg["language"] = language
        pkg["engine"] = "RHVoice"
        pkg["gender"] = _guess_gender(voice_name)

    if rhvoice_pkgs:
        result["RHVoice"] = rhvoice_pkgs

    # ── Piper voices ──
    piper_pkgs = _query_packages("piper-voices-", "piper-voices-")
    # Filter out "piper-voices-common"
    piper_pkgs = [p for p in piper_pkgs if p["pkg"] != "piper-voices-common"]
    for pkg in piper_pkgs:
        locale = pkg["pkg"].removeprefix("piper-voices-")
        pkg["voice_name"] = locale
        pkg["display_name"] = _LANG_DISPLAY.get(locale.lower(), locale.upper())
        pkg["language"] = locale.lower()
        pkg["engine"] = "Piper"
        pkg["gender"] = ""

    if piper_pkgs:
        result["Piper"] = piper_pkgs

    # ── espeak-ng (single package) ──
    espeak_pkgs = _query_packages("espeak-ng", "espeak-ng")
    # Only the main espeak-ng package, not espeakup etc.
    espeak_pkgs = [p for p in espeak_pkgs if p["pkg"] == "espeak-ng"]
    for pkg in espeak_pkgs:
        pkg["voice_name"] = "espeak-ng"
        pkg["display_name"] = "espeak-ng"
        pkg["language"] = "multi"
        pkg["engine"] = "espeak-ng"
        pkg["gender"] = ""

    if espeak_pkgs:
        result["espeak-ng"] = espeak_pkgs

    # ── Also check piper-tts-bin ──
    piper_engine_pkgs = _query_packages("piper-tts", "piper-tts")
    for pkg in piper_engine_pkgs:
        pkg["voice_name"] = "piper-tts"
        pkg["display_name"] = "Piper TTS Engine"
        pkg["language"] = ""
        pkg["engine"] = "Piper (Engine)"
        pkg["gender"] = ""
    if piper_engine_pkgs:
        result.setdefault("Piper", [])
        # Prepend engine packages
        result["Piper"] = piper_engine_pkgs + result["Piper"]

    return result


def _guess_gender(name: str) -> str:
    """Best-effort gender guess from voice name."""
    female = {
        "leticia", "natalia", "anna", "elena", "irina", "lyubov",
        "marianna", "hana", "suze", "magda", "clb", "slt", "spomenka",
        "natia", "arina", "tatiana", "victoria", "alicja", "karmela",
        "dragana", "nazgul", "dilnavoz", "sevinch", "jasietka",
    }
    name_lower = name.lower().replace("-", "")
    for f in female:
        if f in name_lower:
            return "female"
    return "male"


# ── Dialog ───────────────────────────────────────────────────────────


class VoiceManagerDialog(Adw.Dialog):
    """Full-screen Adwaita dialog for managing TTS voice packages."""

    def __init__(
        self,
        on_voices_changed: Callable[[], None] | None = None,
        engine_filter: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._on_voices_changed = on_voices_changed
        self._engine_filter = engine_filter
        self._all_packages: dict[str, list[dict[str, str]]] = {}
        self._busy = False

        self.set_title(_("Voice Manager"))
        self.set_content_width(580)
        self.set_content_height(680)

        # Main layout
        toolbarview = Adw.ToolbarView()

        # Header bar
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(True)
        toolbarview.add_top_bar(header)

        # Scrollable content
        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroll.set_vexpand(True)

        # Container
        self._content_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
        )
        self._content_box.set_margin_start(16)
        self._content_box.set_margin_end(16)
        self._content_box.set_margin_top(12)
        self._content_box.set_margin_bottom(24)

        # Loading state
        self._loading_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
        )
        self._loading_box.set_valign(Gtk.Align.CENTER)
        self._loading_box.set_vexpand(True)
        self._spinner = Gtk.Spinner()
        self._spinner.set_size_request(32, 32)
        self._spinner.set_halign(Gtk.Align.CENTER)
        self._loading_box.append(self._spinner)
        loading_label = Gtk.Label(label=_("Loading available voices…"))
        loading_label.add_css_class("dim-label")
        self._loading_box.append(loading_label)

        # Stack
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.add_named(self._loading_box, "loading")
        self._scroll.set_child(self._content_box)
        self._stack.add_named(self._scroll, "content")

        toolbarview.set_content(self._stack)
        self.set_child(toolbarview)

        # Start loading
        self._stack.set_visible_child_name("loading")
        self._spinner.start()
        threading.Thread(target=self._load_packages, daemon=True).start()

    # ── Loading ──────────────────────────────────────────────────────

    def _load_packages(self) -> None:
        """Load all packages in background."""
        data = _query_all_voice_packages()
        GLib.idle_add(self._populate, data)

    def _populate(self, data: dict[str, list[dict[str, str]]]) -> None:
        """Populate UI (main thread)."""
        self._all_packages = data
        self._spinner.stop()

        if not data:
            self._show_empty_state()
        else:
            self._rebuild_list()

        self._stack.set_visible_child_name("content")

    def _show_empty_state(self) -> None:
        """Show when no packages found."""
        self._clear_content()
        status = Adw.StatusPage()
        status.set_icon_name("dialog-warning-symbolic")
        status.set_title(_("No Voice Packages Found"))
        status.set_description(
            _("Could not find any TTS voice packages from pacman repositories.")
        )
        self._content_box.append(status)

    def _clear_content(self) -> None:
        """Remove all children."""
        child = self._content_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._content_box.remove(child)
            child = nxt

    # ── Build UI ─────────────────────────────────────────────────────

    def _rebuild_list(self) -> None:
        """Build the full engine-grouped voice list."""
        self._clear_content()

        engine_meta = {
            "RHVoice": {
                "icon": "audio-speakers-symbolic",
                "subtitle": _("High quality offline voices"),
            },
            "Piper": {
                "icon": "starred-symbolic",
                "subtitle": _("Neural network voices — natural sounding"),
            },
            "espeak-ng": {
                "icon": "audio-card-symbolic",
                "subtitle": _("Lightweight multi-language synthesizer"),
            },
        }

        for engine_name in ["RHVoice", "Piper", "espeak-ng"]:
            # Apply engine filter if specified
            if self._engine_filter and self._engine_filter.lower() not in engine_name.lower():
                continue

            pkgs = self._all_packages.get(engine_name, [])
            if not pkgs:
                continue

            meta = engine_meta.get(engine_name, {})
            installed = [p for p in pkgs if p["installed"] == "yes"]
            available = [p for p in pkgs if p["installed"] == "no"]

            # ── Engine group ──
            group = Adw.PreferencesGroup()
            group.set_title(engine_name)
            group.set_description(meta.get("subtitle", ""))

            # ── Installed sub-section ──
            if installed:
                for pkg in sorted(installed, key=lambda p: p.get("display_name", "")):
                    row = self._make_row(pkg, is_installed=True)
                    group.add(row)

            # ── Available sub-section ──
            if available:
                if engine_name == "RHVoice":
                    # Group by language in expanders
                    by_lang: dict[str, list[dict[str, str]]] = {}
                    for pkg in available:
                        by_lang.setdefault(pkg["language"], []).append(pkg)

                    expander = Adw.ExpanderRow()
                    expander.set_title(
                        _("Add RHVoice — {count} available").format(count=len(available))
                    )
                    expander.set_subtitle(
                        _("{langs} languages").format(langs=len(by_lang))
                    )
                    expander.set_icon_name("list-add-symbolic")

                    for lang in sorted(by_lang.keys()):
                        lang_display = _LANG_DISPLAY.get(lang, lang.title())
                        lang_exp = Adw.ExpanderRow()
                        lang_exp.set_title(lang_display)
                        lang_exp.set_subtitle(
                            _("{count} voice(s)").format(count=len(by_lang[lang]))
                        )

                        for pkg in sorted(by_lang[lang], key=lambda p: p["display_name"]):
                            row = self._make_row(pkg, is_installed=False)
                            lang_exp.add_row(row)

                        expander.add_row(lang_exp)

                    group.add(expander)

                elif engine_name == "Piper":
                    expander = Adw.ExpanderRow()
                    expander.set_title(
                        _("Add Piper voices — {count} available").format(count=len(available))
                    )
                    expander.set_subtitle(_("Neural TTS voice packs by language"))
                    expander.set_icon_name("list-add-symbolic")

                    for pkg in sorted(available, key=lambda p: p.get("display_name", "")):
                        row = self._make_row(pkg, is_installed=False)
                        expander.add_row(row)

                    group.add(expander)

                else:
                    # espeak-ng — single package
                    for pkg in available:
                        row = self._make_row(pkg, is_installed=False)
                        group.add(row)

            self._content_box.append(group)

    def _make_row(
        self, pkg: dict[str, str], *, is_installed: bool
    ) -> Adw.ActionRow:
        """Create a row with install/remove button."""
        row = Adw.ActionRow()

        gender = pkg.get("gender", "")
        gender_icon = _GENDER_ICON.get(gender, "")
        display = pkg.get("display_name", pkg["pkg"])

        if gender_icon:
            row.set_title(f"{gender_icon}  {display}")
        else:
            row.set_title(display)

        if is_installed:
            lang = pkg.get("language", "")
            lang_display = _LANG_DISPLAY.get(lang, lang.title()) if lang else ""
            
            # For Piper, title is often the language display name. Avoid repeating.
            parts = []
            if lang_display and lang_display.strip().lower() != display.strip().lower():
                parts.append(lang_display)
            if pkg.get("version"):
                parts.append(pkg["version"])
            
            if parts:
                row.set_subtitle("  •  ".join(parts))

            # Installed badge
            badge = Gtk.Label(label=_("Installed"))
            badge.add_css_class("voice-manager-badge")
            badge.add_css_class("voice-manager-installed-badge")
            badge.set_valign(Gtk.Align.CENTER)
            row.add_suffix(badge)

            # Only show Remove for voice packages, not core engines
            if pkg["pkg"] not in ("espeak-ng", "piper-tts-bin", "piper-voices-common"):
                btn = Gtk.Button()
                btn_content = Adw.ButtonContent()
                btn_content.set_icon_name("user-trash-symbolic")
                btn_content.set_label(_("Remove"))
                btn.set_child(btn_content)
                btn.set_valign(Gtk.Align.CENTER)
                btn.add_css_class("flat")
                btn.set_tooltip_text(
                    _("Remove {name}").format(name=display)
                )
                btn.connect("clicked", lambda b, p=pkg: self._on_remove(b, p))
                row.add_suffix(btn)
        else:
            lang = pkg.get("language", "")
            lang_display = _LANG_DISPLAY.get(lang, lang.title()) if lang else ""
            
            # Avoid duplicating title and subtitle if they are essentially the same (e.g. Piper voices)
            if lang_display and lang_display.strip().lower() != display.strip().lower():
                row.set_subtitle(lang_display)

            btn = Gtk.Button()
            btn_content = Adw.ButtonContent()
            btn_content.set_icon_name("list-add-symbolic")
            btn_content.set_label(_("Install"))
            btn.set_child(btn_content)
            btn.set_valign(Gtk.Align.CENTER)
            btn.add_css_class("suggested-action")
            btn.set_tooltip_text(
                _("Install {name}").format(name=display)
            )
            btn.connect("clicked", lambda b, p=pkg: self._on_install(b, p))
            row.add_suffix(btn)

        return row

    # ── Actions ──────────────────────────────────────────────────────

    def _on_install(self, button: Gtk.Button, pkg: dict[str, str]) -> None:
        """Install a package after confirmation."""
        if self._busy:
            return

        display = pkg.get("display_name", pkg["pkg"])
        self._confirm(
            heading=_("Install Voice"),
            body=_(
                "Install <b>{name}</b>?\n\nThis requires administrator privileges."
            ).format(name=display),
            confirm_label=_("Install"),
            appearance=Adw.ResponseAppearance.SUGGESTED,
            on_confirm=lambda: self._run_action("install", pkg, button),
        )

    def _on_remove(self, button: Gtk.Button, pkg: dict[str, str]) -> None:
        """Remove a package after confirmation."""
        if self._busy:
            return

        display = pkg.get("display_name", pkg["pkg"])
        self._confirm(
            heading=_("Remove Voice"),
            body=_(
                "Remove <b>{name}</b>?\n\nYou can reinstall it later."
            ).format(name=display),
            confirm_label=_("Remove"),
            appearance=Adw.ResponseAppearance.DESTRUCTIVE,
            on_confirm=lambda: self._run_action("remove", pkg, button),
        )

    def _confirm(
        self,
        heading: str,
        body: str,
        confirm_label: str,
        appearance: Adw.ResponseAppearance,
        on_confirm: Callable[[], None],
    ) -> None:
        """Confirmation dialog."""
        dialog = Adw.AlertDialog()
        dialog.set_heading(heading)
        dialog.set_body(body)
        dialog.set_body_use_markup(True)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("confirm", confirm_label)
        dialog.set_response_appearance("confirm", appearance)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_resp(d: Adw.AlertDialog, response: str) -> None:
            if response == "confirm":
                on_confirm()

        dialog.connect("response", _on_resp)
        parent = self.get_root()
        dialog.present(parent if parent else self)

    def _run_action(
        self, action: str, pkg: dict[str, str], button: Gtk.Button
    ) -> None:
        """Execute pacman install/remove in background."""
        self._busy = True
        button.set_sensitive(False)

        # Show spinner on button
        spinner = Gtk.Spinner()
        spinner.set_size_request(16, 16)
        spinner.start()
        old_child = button.get_child()
        button.set_child(spinner)

        pkg_name = pkg["pkg"]

        def _worker() -> tuple[bool, str]:
            try:
                if action == "install":
                    cmd = [
                        "pkexec", "pacman", "-S", "--noconfirm",
                        "--needed", pkg_name,
                    ]
                else:
                    cmd = [
                        "pkexec", "pacman", "-R", "--noconfirm", pkg_name,
                    ]

                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                if proc.returncode == 0:
                    return True, ""
                err = proc.stderr.strip() or proc.stdout.strip()
                return False, err
            except subprocess.TimeoutExpired:
                return False, _("Operation timed out")
            except FileNotFoundError:
                return False, _("pkexec or pacman not found")
            except Exception as e:
                return False, str(e)

        def _on_done(result: tuple[bool, str]) -> bool:
            success, error = result
            self._busy = False
            button.set_child(old_child)
            button.set_sensitive(True)

            if success:
                # Reload the full list
                self._spinner.start()
                self._stack.set_visible_child_name("loading")
                threading.Thread(target=self._load_packages, daemon=True).start()

                if self._on_voices_changed:
                    self._on_voices_changed()
            else:
                err_dialog = Adw.AlertDialog()
                err_dialog.set_heading(
                    _("Failed") if action == "install"
                    else _("Removal Failed")
                )
                err_dialog.set_body(error[:500] if error else _("Unknown error"))
                err_dialog.add_response("ok", _("OK"))
                parent = self.get_root()
                err_dialog.present(parent if parent else self)

            return False

        def _threaded() -> None:
            result = _worker()
            GLib.idle_add(_on_done, result)

        threading.Thread(target=_threaded, daemon=True).start()
