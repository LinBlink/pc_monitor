#!/bin/sh
# HELP: Stream a PC's CPU / GPU / RAM / network dashboard over WiFi.
# ICON: pcmonitor
# GRID: PC Monitor

# muOS's entry point for an application: the frontend runs this with the app's
# own directory as $1, and shows the HELP/GRID text above in the menu.
#
# It is a wrapper rather than the launcher itself for one reason: muOS's quit
# path does `pidof "$(GET_VAR system foreground_process)"`, and the kernel sets
# a script's comm to its own basename - so every muOS app would answer to
# `pidof mux_launch.sh`. Exec'ing the real launcher under its own name gives
# muOS something unambiguous to signal, and pcmonitor.sh already exits cleanly
# on SIGTERM.

. /opt/muos/script/var/func.sh

SETUP_APP "pcmonitor.sh" ""

APPDIR="${1:-$(dirname "$0")}"
cd "$APPDIR" || exit 1

exec ./pcmonitor.sh
