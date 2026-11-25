#!/bin/bash
# End-to-end check on Window Maker (X11), isolated from the running session.
#
# Xvfb + the real wmaker with Big WindowMaker's default preferences, the
# ABNT2 keyboard, a private HOME and a private session bus (dbus-run-session),
# and the X11 tray of Big WindowMaker (snixembed + wmsystemtray) when they
# are installed. Then, as a user would:
#
#   1. biglinux-tts --repair-shortcut   → WMRootMenu gets the SHORTCUT item
#   2. text selected in X11 (PRIMARY)   → xsel -p -i
#   3. Alt+V pressed (XTEST)            → wmaker runs biglinux-tts-speak
#   4. the app reads it                 → MPRIS PlaybackStatus "Playing"
#   5. Alt+V again while reading        → "Stopped"
#   6. the tray shows the icon          → in wmsystemtray (XEmbed) or via snixembed (SNI)
#   7. nothing selected + Alt+V         → nothing is read
#
# Audio is really played (one short sentence). Needs Xvfb, wmaker, xdotool,
# xsel, xprop, setxkbmap. Usage:  bash tests/integration/x11_wmaker_e2e.sh
set -u
if [[ -z "${TTS_E2E_INNER:-}" ]]; then
    export TTS_E2E_INNER=1
    exec dbus-run-session -- bash "$0" "$@"
fi

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
T="$(mktemp -d)"
DISP=":47"
fail=0
cleanup() {
    kill "${PIDS[@]}" 2>/dev/null
    pkill -f "Xvfb $DISP" 2>/dev/null
    rm -rf "$T"
}
PIDS=()
trap cleanup EXIT

export HOME="$T" XDG_CONFIG_HOME="$T/.config" GNUSTEP_USER_ROOT="$T/GNUstep"
export DISPLAY="$DISP" XDG_SESSION_TYPE=x11 XDG_CURRENT_DESKTOP=WindowMaker DESKTOP_SESSION=big-wm
export GDK_BACKEND=x11 QT_QPA_PLATFORM=xcb BIGLINUX_TTS_DEBUG=1
unset WAYLAND_DISPLAY
mkdir -p "$T/GNUstep/Defaults" "$T/.config"
cp /usr/share/big-wm/GNUstep/Defaults/{WindowMaker,WMGLOBAL,WMRootMenu,WMState,WMWindowAttributes} "$T/GNUstep/Defaults/" 2>/dev/null

ok() { echo "  ✔ $*"; }
ko() { echo "  ✘ $*"; fail=1; }
playback() {
    gdbus call --session --dest org.mpris.MediaPlayer2.biglinux-tts --object-path /org/mpris/MediaPlayer2 \
        --method org.freedesktop.DBus.Properties.Get org.mpris.MediaPlayer2.Player PlaybackStatus 2>/dev/null |
        sed -n "s/.*'\(.*\)'.*/\1/p"
}
playing() { [[ "$(playback)" == Playing ]]; }
not_playing() { [[ "$(playback)" != Playing ]]; }
tray_item() {
    gdbus call --session --dest org.kde.StatusNotifierWatcher --object-path /StatusNotifierWatcher \
        --method org.freedesktop.DBus.Properties.Get org.kde.StatusNotifierWatcher RegisteredStatusNotifierItems \
        2>/dev/null | grep -q StatusNotifierItem
}
wait_for() { # seconds, command... (the command is run again at each try)
    local n=$(( $1 * 10 )); shift
    for ((i = 0; i < n; i++)); do "$@" && return 0; sleep 0.1; done
    return 1
}

Xvfb "$DISP" -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 & PIDS+=($!)
wait_for 5 xprop -display "$DISP" -root _NET_SUPPORTED || { echo "Xvfb did not start"; exit 1; }
setxkbmap -display "$DISP" -model abnt2 -layout br
# The app started by Window Maker logs to wmaker's stderr.
wmaker --no-autolaunch >/dev/null 2>"$T/app.log" & PIDS+=($!)
sleep 2
echo "Window Maker on Xvfb $DISP (br/abnt2), HOME=$T"

if command -v snixembed >/dev/null && command -v wmsystemtray >/dev/null; then
    wmsystemtray >/dev/null 2>&1 & PIDS+=($!)
    sleep 0.5
    snixembed >/dev/null 2>&1 & PIDS+=($!)
    sleep 1
fi

echo "1. register"
"$REPO/usr/bin/biglinux-tts" --repair-shortcut | sed 's/^/     /'
grep -q 'SHORTCUT, "Mod1+v"' "$T/GNUstep/Defaults/WMRootMenu" && ok "WMRootMenu has Mod1+v" || ko "WMRootMenu not written"
sleep 3  # Window Maker notices the changed defaults and rebinds

echo "2-4. select + Alt+V"
printf 'Texto selecionado no Window Maker, com acentuação, lido em voz alta para conferir o atalho global.' | xsel -p -i
t0=$(date +%s%N)
xdotool key alt+v
if wait_for 20 playing; then
    ok "reading after $(( ($(date +%s%N) - t0) / 1000000 )) ms (cold start)"
else
    ko "nothing read"
fi

echo "5. Alt+V again, while reading"
t0=$(date +%s%N)
xdotool key alt+v
wait_for 5 not_playing && ok "stopped after $(( ($(date +%s%N) - t0) / 1000000 )) ms" || ko "did not stop"

echo "6. tray"
if pgrep -x snixembed >/dev/null; then
    if wait_for 5 tray_item; then
        ok "icon registered as StatusNotifierItem (snixembed → wmsystemtray)"
    elif xwininfo -root -tree | grep -A3 -i wmsystemtray | grep -q '"BigLinux TTS"'; then
        ok "icon embedded in wmsystemtray (XEmbed)"
    else
        ko "no tray icon"; xwininfo -root -tree | grep -iE 'wmsystemtray|BigLinux|python' | head
    fi
else
    echo "     (snixembed/wmsystemtray not installed: skipped)"
fi

echo "7. nothing selected"
xsel -p -c
sleep 0.5
xdotool key alt+v
sleep 2
[[ "$(playback)" != Playing ]] && ok "nothing read" || ko "read something with no selection"

if ((fail)); then
    echo "--- bus names"
    gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
        --method org.freedesktop.DBus.ListNames 2>&1 | tr ',' '\n' | grep -vE ":1\." | head -20
    gdbus call --session --dest org.mpris.MediaPlayer2.biglinux-tts --object-path /org/mpris/MediaPlayer2 \
        --method org.freedesktop.DBus.Properties.Get org.mpris.MediaPlayer2.Player PlaybackStatus 2>&1 | head -3
    echo "--- app log"
    grep -vE 'Icon .* not found' "$T/app.log" | tail -40
fi
exit $fail
