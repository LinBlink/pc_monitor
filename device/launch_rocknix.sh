#!/bin/bash
# PC Monitor - ROCKNIX launcher (tested on Powkiddy X55 / RK3566).
#
# All rendering happens on the PC; the handheld only decodes the MJPEG stream
# full-screen. Same workflow as the Miyoo launch.sh: discover PCs on the LAN,
# switch between them, rotate the layout, quit, report battery.
#
# Controls
#     LEFT / RIGHT   prev / next discovered PC
#     UP / DOWN      turn the page (overview / detail)
#     Y              rotate, 4 positions (landscape / portrait / both upside down)
#     SELECT         quit back to EmulationStation
#
# Three things differ from the Miyoo port, and each one is a thing the earlier
# attempt at this file got wrong:
#
#   * ROCKNIX ships GNU coreutils, not busybox applets, on $PATH. `timeout -t 2`
#     is busybox syntax and exits 125 without ever running the command, which
#     turned the button reader into a spin loop that never read a byte.
#   * This panel is not mounted upside down, so it does not want the Miyoo's
#     180-degree pre-rotation - but it cancels that here rather than asking the
#     PC to stop doing it, see PANEL_FLIP below. sway drives DSI-1 with
#     transform=270, so the logical output is 960x720 landscape, exactly 1.5x
#     the 640x480 frame, which mpv scales to fill the screen.
#   * ffplay is not packaged with ROCKNIX, so the player is mpv.

set -u

here=$(cd "$(dirname "$0")" && pwd)
progdir="$here/pcmonitor"
mkdir -p "$progdir"

log="$progdir/pcmonitor.log"
hosts="$progdir/hosts.txt"
state="$progdir/state.cfg"
cmdfile=/tmp/pcmon_cmd
stopflag=/tmp/pcmon_stop
sweepflag=/tmp/pcmon_sweep
pidfile=/tmp/pcmon_player
targetfile=/tmp/pcmon_target
audiofile=/tmp/pcmon_advice
selfpid="$progdir/.pid"

# EmulationStation launches ports through runemu.sh, which does not necessarily
# pass the session's Wayland variables down. mpv is Wayland-only on this build
# (it is compiled without EGL, so the GPU path is Vulkan on wayland), and it
# exits immediately without a compositor to talk to. XDG_RUNTIME_DIR is the
# root user's, and the compositor socket is wayland-1 rather than the usual
# wayland-0 - but glob for it rather than hardcoding, since that is exactly the
# kind of detail that changes between ROCKNIX builds.
: "${XDG_RUNTIME_DIR:=/var/run/0-runtime-dir}"
if [ -z "${WAYLAND_DISPLAY:-}" ] || [ ! -S "$XDG_RUNTIME_DIR/${WAYLAND_DISPLAY:-none}" ]; then
    for sock in "$XDG_RUNTIME_DIR"/wayland-[0-9]*; do
        case "$sock" in *.lock) continue ;; esac
        [ -S "$sock" ] && WAYLAND_DISPLAY=$(basename "$sock") && break
    done
fi
export XDG_RUNTIME_DIR WAYLAND_DISPLAY

# Button indices on /dev/input/js0. The rocknix-joypad driver publishes both
# js0 and event3; js0 is used because it needs no evdev keycode table - joydev
# numbers the buttons in ascending keycode order, so for this pad's 17 keys
# (BTN_SOUTH..BTN_THUMBR, then BTN_DPAD_UP..RIGHT) the indices below are fixed
# by the driver's key bitmap, not by guesswork.
JSBTN_Y=3            # BTN_WEST
JSBTN_SELECT=8       # BTN_SELECT
# BTN_DPAD_UP/DOWN/LEFT/RIGHT are consecutive evdev codes, so they land as
# consecutive js0 button indices too.
JSBTN_UP=13          # BTN_DPAD_UP
JSBTN_DOWN=14        # BTN_DPAD_DOWN
JSBTN_LEFT=15        # BTN_DPAD_LEFT
JSBTN_RIGHT=16       # BTN_DPAD_RIGHT

PC_HOST=192.168.2.114
PC_PORT=8765
STREAM_FPS=8
DISCOVER_EVERY_S=120
BATT_EVERY_S=60
SPEAK_EVERY_S=60
PANEL_FLIP=0
[ -f "$progdir/settings.cfg" ] && . "$progdir/settings.cfg"

# The PC pre-rotates every frame 180 degrees when its own `rotate180` setting is
# on, because the Miyoo's panel is mounted upside down and this one is not. That
# setting is per-PC and the Miyoo needs it left on, so the fix has to happen
# here.
#
# It happens as arithmetic rather than as a request, because a request only
# works against a PC running a build that understands it - and a fleet is never
# all on the same build at once. A 180-degree flip *is* two quarter-turns:
# orient maps to {0:0, 1:270, 2:180, 3:90} degrees, so orient+2 is always
# exactly orient plus 180, and it keeps the same parity, hence the same layout.
# So asking for orient+2 cancels a flip we do not want, on any PC, old or new,
# with nothing to deploy at the other end.
compensated_orient() {
    if [ "$1" != "$PANEL_FLIP" ]; then
        echo $(((ORIENT + 2) % 4))
    else
        echo "$ORIENT"
    fi
}

# Quarter-turns clockwise, 0-3, same as the Miyoo: 0 and 2 are the landscape
# layout the right way up and upside down, 1 and 3 the portrait layout turned
# each way. Every one of the four is reachable with Y, because which one reads
# correctly depends on the panel and on how the device is held - and sway hands
# a fullscreen surface straight to the hardware plane, which does not always
# apply the output's own 270-degree transform.
IDX=0
ORIENT=0
PAGE=0
# How many pages the PC draws. Replaced by whatever /config.json reports
# on each connect; this is only what to assume before the first answer.
PAGES=2
[ -f "$state" ] && . "$state"

: > "$log"
# Everything the shell itself has to say - mpv's "Killed" whenever the button
# reader stops the player, and any error from the script - belongs in our own
# log rather than in EmulationStation's exec.log.
exec 2>> "$log"
say() { echo "$(date '+%H:%M:%S') $*" >> "$log"; }

save_state() {
    printf 'IDX=%s\nORIENT=%s\nPAGE=%s\n' "$IDX" "$ORIENT" "$PAGE" > "$state"
}

# Both directions exist even though there are only two pages today: UP and
# DOWN are how you would expect to walk a list, and the count comes from
# the PC rather than being fixed here.
turn_page() {
    [ "$PAGES" -ge 1 ] || PAGES=1
    PAGE=$(((PAGE + PAGES + $1) % PAGES))
    save_state
}

host_count() {
    c=$(wc -l < "$hosts" 2>/dev/null)
    [ -z "$c" ] && c=0
    echo "$c"
}

# The PC draws the device switcher, but only the handheld knows what is on the
# LAN, so the names ride along in the stream URL. Anything outside the URL-safe
# set is dropped rather than escaped; these are hostnames, and a name that
# survives is worth more than a faithful one.
device_list() {
    devs=""
    while IFS='|' read -r ip nm; do
        [ -n "$ip" ] || continue
        [ -n "$nm" ] || nm=$ip
        nm=$(printf '%s' "$nm" | tr -cd 'A-Za-z0-9._-')
        [ -n "$nm" ] || nm=$ip
        devs="${devs:+$devs,}$nm"
    done < "$hosts"
    echo "$devs"
}

# --- discovery -------------------------------------------------------------
# A TCP-connect sweep of the /24 with 32 in flight covers the subnet in a few
# seconds, which is fast enough to run in the background while the remembered
# host is already on screen.
discover() {
    # Whatever interface carries the default route - wlan0 today, but an
    # ethernet dock or a differently named wifi driver should still work.
    dev=$(ip -4 route show default 2>/dev/null | awk '{print $5; exit}')
    myip=$(ip -4 -o addr show ${dev:+dev "$dev"} scope global 2>/dev/null |
           awk '{print $4}' | cut -d/ -f1 | head -1)
    if [ -z "$myip" ]; then
        say "discover: no LAN address"
        return
    fi
    net=${myip%.*}
    open=/tmp/pcmon_open
    found=/tmp/pcmon_found
    : > "$open"
    : > "$found"

    n=0
    i=1
    while [ $i -le 254 ]; do
        ( nc -w 1 "$net.$i" "$PC_PORT" </dev/null >/dev/null 2>&1 &&
          echo "$net.$i" >> "$open" ) &
        n=$((n + 1))
        i=$((i + 1))
        if [ $n -ge 32 ]; then
            wait
            n=0
        fi
    done
    wait
    say "discover: swept $net.0/24, $(wc -l < "$open") port(s) open"

    # An open port is not proof of a PC Monitor: only hosts that answer
    # /config.json with a name are kept, and the name is what gets listed.
    for ip in $(sort -t. -k4 -n "$open" 2>/dev/null || cat "$open"); do
        name=$(curl -s -m 3 "http://$ip:$PC_PORT/config.json" |
               jq -r '.name // empty' 2>/dev/null)
        if [ -n "$name" ]; then
            echo "$ip|$name" >> "$found"
        else
            say "discover: $ip open but not a PC Monitor"
        fi
    done

    if [ -s "$found" ]; then
        # Compared in the shell because ROCKNIX ships no `cmp` and no `diff`;
        # a missing command exits non-zero, which would read as "the list
        # changed" and drop the stream on every single sweep.
        if [ "$(cat "$found")" = "$(cat "$hosts")" ]; then
            say "discover: found $(host_count) device(s), unchanged"
        else
            cp -f "$found" "$hosts"
            say "discover: found $(host_count) device(s): $(tr '\n' ' ' < "$hosts")"
            echo refresh > "$cmdfile"
            [ -s "$pidfile" ] && kill -9 "$(cat "$pidfile")" 2>/dev/null
        fi
    else
        say "discover: nothing found, keeping $(host_count) known"
    fi
}

# The original sweep ran once at launch, so a PC that started up afterwards
# never appeared in the switcher — you had to quit and relaunch to see it. This
# re-sweeps on a timer instead. It is cheap to do so: discover() compares the
# result with the current list and only interrupts the stream when it actually
# changed. The wait is in one-second steps so quitting stays instant, and a
# touch of the sweep flag cuts it short when the current PC has gone quiet.
discover_loop() {
    while [ ! -f "$stopflag" ]; do
        discover
        i=0
        while [ "$i" -lt "$DISCOVER_EVERY_S" ] && [ ! -f "$stopflag" ]; do
            if [ -f "$sweepflag" ]; then
                rm -f "$sweepflag"
                break
            fi
            sleep 1
            i=$((i + 1))
        done
    done
}

# --- battery ---------------------------------------------------------------
# The dashboard is drawn on the PC, so the handheld's own charge level has to
# be sent there: /battery?pct=..&charging=.. once a minute, attributed by the
# PC to the address it came from. ROCKNIX exposes the rk817 PMIC through the
# standard power_supply sysfs class.
BATT_SYS=/sys/class/power_supply/battery

battery_read() {
    BATT_PCT=$(cat "$BATT_SYS/capacity" 2>/dev/null)
    BATT_CHG=0
    case "$(cat "$BATT_SYS/status" 2>/dev/null)" in
        Charging | Full) BATT_CHG=1 ;;
    esac
    case "$BATT_PCT" in
        '' | *[!0-9]*) BATT_PCT="" ;;
    esac
}

battery_loop() {
    while [ ! -f "$stopflag" ]; do
        battery_read
        target=$(cat "$targetfile" 2>/dev/null)
        if [ -n "$BATT_PCT" ] && [ -n "$target" ]; then
            curl -s -m 3 -o /dev/null \
                "$target/battery?pct=$BATT_PCT&charging=$BATT_CHG"
        fi

        # Sleep in one-second steps so quitting does not wait out the interval.
        i=0
        while [ $i -lt "$BATT_EVERY_S" ] && [ ! -f "$stopflag" ]; do
            sleep 1
            i=$((i + 1))
        done
    done
}

# --- spoken advice ---------------------------------------------------------
# The PC decides everything: whether there is advice, whether it is worth saying
# out loud, and what it sounds like. All this does is poll the PC that is
# currently on screen, and play the clip once per advice id - which is also what
# makes the announcement follow the device switcher rather than the LAN. Switch
# to another PC and you hear that PC's advice, not this one's.
# mpv is already the video player here, so it is also the one certain to be
# installed and to understand the mp3 the PC sends.
speak_play() {
    mpv --no-video --really-quiet --no-input-default-bindings \
        --input-vo-keyboard=no "$1" >> "$log" 2>&1
}

speak_loop() {
    spoken=""
    seen_target=""
    while [ ! -f "$stopflag" ]; do
        target=$(cat "$targetfile" 2>/dev/null)
        if [ -n "$target" ]; then
            # A different PC has its own advice numbering, so the "already said
            # this one" memory has to be per PC or the first advice from the
            # machine you just switched to would be swallowed.
            if [ "$target" != "$seen_target" ]; then
                seen_target=$target
                spoken=""
            fi
            js=$(curl -s -m 5 "$target/advice.json")
            id=$(printf '%s' "$js" | jq -r '.id // empty' 2>/dev/null)
            say_it=$(printf '%s' "$js" |
                jq -r 'if .speak then 1 else 0 end' 2>/dev/null)
            if [ -n "$id" ] && [ "$say_it" = "1" ] && [ "$id" != "$spoken" ]; then
                if curl -s -m 20 -f -o "$audiofile" "$target/advice.audio"; then
                    spoken=$id
                    say "advice $id from $target, speaking"
                    speak_play "$audiofile"
                else
                    say "advice $id has no audio yet"
                fi
            fi
        fi

        i=0
        while [ $i -lt "$SPEAK_EVERY_S" ] && [ ! -f "$stopflag" ]; do
            sleep 1
            i=$((i + 1))
        done
    done
}

# --- button reader ---------------------------------------------------------
# Writes an action then kills mpv, in that order, so the main loop always finds
# the action already waiting when mpv returns.
#
# A js_event is 8 bytes: time(4) value(2 LE) type(1) number(1), so `od -tu1 -w8`
# prints one event per line as fields $1..$8 - $7 is the type and $8 the button.
# `-v` is not optional: without it od collapses a line identical to the one
# before into a bare `*`, and two presses of the same button in the same second
# produce exactly that.
#
# The read is wrapped in a 2-second timeout instead of blocking forever so the
# loop can notice the stop flag and exit on its own. A blocked `dd` orphaned by
# a kill would keep its own copy of the js0 queue open, and the next launch
# would then lose half its events to the leftover reader.
keyreader() {
    exec 3< /dev/input/js0
    while [ ! -f "$stopflag" ]; do
        ev=$(timeout 2 dd bs=8 count=1 2>/dev/null <&3 | od -An -tu1 -w8 -v)
        [ -z "$ev" ] && continue
        set -- $ev
        [ "$#" -eq 8 ] || continue
        # type 1 is JS_EVENT_BUTTON; the 0x80 bit marks the synthetic burst of
        # initial states the driver sends when the device is opened.
        [ "$7" = "1" ] || continue
        [ "$5" = "1" ] && [ "$6" = "0" ] || continue    # press, not release
        case "$8" in
            "$JSBTN_Y") echo rotate > "$cmdfile" ;;
            "$JSBTN_SELECT") echo quit > "$cmdfile" ;;
            "$JSBTN_LEFT") echo prev > "$cmdfile" ;;
            "$JSBTN_RIGHT") echo next > "$cmdfile" ;;
            "$JSBTN_UP") echo pageup > "$cmdfile" ;;
            "$JSBTN_DOWN") echo pagedown > "$cmdfile" ;;
            *) say "btn $8 (unbound)"; continue ;;
        esac
        say "btn $8 -> $(cat $cmdfile)"
        [ -s "$pidfile" ] && kill -9 "$(cat "$pidfile")" 2>/dev/null
    done
}

take_cmd() {
    CMD=""
    if [ -s "$cmdfile" ]; then
        CMD=$(cat "$cmdfile")
        : > "$cmdfile"
    fi
}

# Runs the player in the background and records its pid so the button reader
# can stop exactly this process, then waits for it and returns its status.
#
# --untimed with the caches off is what makes this a monitor rather than a
# video player: mpv draws each JPEG the moment it arrives instead of pacing it
# against a clock, so the picture stays a frame behind the PC rather than the
# 1.4 seconds of demuxer buffer the defaults accumulate. --fs fills the screen;
# the input options keep mpv from binding the gamepad, which this script reads
# itself.
play() {
    mpv --fs --no-audio --no-osc --no-osd-bar --really-quiet \
        --no-input-default-bindings --input-vo-keyboard=no --input-cursor=no \
        --profile=low-latency --untimed --cache=no --demuxer-readahead-secs=0 \
        --demuxer-lavf-format=mjpeg --demuxer-lavf-o-add="framerate=$2" \
        --vo=gpu --keep-open=no --idle=no \
        "$1" >> "$log" 2>&1 &
    PLAYER=$!
    echo "$PLAYER" > "$pidfile"
    wait "$PLAYER"
    rc=$?
    : > "$pidfile"
    return $rc
}

# --- stream watchdog -------------------------------------------------------
# mpv does not reliably notice a stream that died under it: when the PC's
# server exits, or WiFi drops, the socket goes away but mpv can sit spinning
# on a frozen frame forever - and the main loop is blocked in `wait`, so
# nothing reconnects until the user quits.
#
# The kernel knows the truth, so look for an established connection to the
# PC's port in /proc/net/tcp and kill the player when there has been none
# for two checks running. Two checks rather than one because the connection
# is legitimately absent for an instant between reconnects, and one check
# may also catch the battery reporter's own short-lived connection.
PORT_HEX=$(printf '%04X' "$PC_PORT")

stream_connected() {
    awk -v p=":$PORT_HEX\$" '$4 == "01" && $3 ~ p { found = 1 }
                              END { exit !found }' \
        /proc/net/tcp 2>/dev/null
}

watchdog() {
    misses=0
    while [ ! -f "$stopflag" ]; do
        sleep 10
        if [ ! -s "$pidfile" ] || stream_connected; then
            misses=0
            continue
        fi
        misses=$((misses + 1))
        if [ "$misses" -ge 2 ]; then
            misses=0
            say "watchdog: no stream connection, restarting player"
            kill -9 "$(cat "$pidfile")" 2>/dev/null
        fi
    done
}

cleanup() {
    touch "$stopflag"
    rm -f "$selfpid"
    [ -s "$pidfile" ] && kill -9 "$(cat "$pidfile")" 2>/dev/null
    for p in "${KR_PID:-}" "${DISC_PID:-}" "${BATT_PID:-}" \
         "${SPEAK_PID:-}" "${WD_PID:-}"; do
        [ -n "$p" ] && kill "$p" 2>/dev/null
    done
    rm -f "$targetfile" "$sweepflag"
}
trap 'cleanup; exit 0' INT TERM

# A previous run that was killed rather than exited would otherwise keep
# reading the input device and fighting over the screen.
if [ -s "$selfpid" ]; then
    prev=$(cat "$selfpid")
    if [ "$prev" != "$$" ] && kill -0 "$prev" 2>/dev/null; then
        say "stopping previous instance $prev"
        kill -TERM "$prev" 2>/dev/null
        sleep 2
        kill -9 "$prev" 2>/dev/null
    fi
fi
echo "$$" > "$selfpid"

# Only now: the /tmp flags are shared between instances, and the previous one
# sets the stop flag as it exits. Clearing these before that happened would
# hand the flag straight to this instance's own helpers, which would exit
# immediately and leave the app running with no buttons and no battery reports.
: > "$cmdfile"
rm -f "$stopflag" "$sweepflag"

# Seed the list with the configured host so the first frame does not wait for
# a subnet sweep; discovery then refines it in the background.
[ -s "$hosts" ] || echo "$PC_HOST|$PC_HOST" > "$hosts"

keyreader &
KR_PID=$!
discover_loop &
DISC_PID=$!
battery_loop &
BATT_PID=$!
speak_loop &
SPEAK_PID=$!
watchdog &
WD_PID=$!

say "start idx=$IDX orient=$ORIENT page=$PAGE panel_flip=$PANEL_FLIP display=$WAYLAND_DISPLAY"

while :; do
    total=$(host_count)
    [ "$total" -lt 1 ] && total=1
    [ "$IDX" -ge "$total" ] && IDX=0
    [ "$IDX" -lt 0 ] && IDX=$((total - 1))

    line=$(sed -n "$((IDX + 1))p" "$hosts" 2>/dev/null)
    host=$(echo "$line" | cut -d'|' -f1)
    [ -z "$host" ] && host=$PC_HOST
    base="http://$host:$PC_PORT"
    echo "$base" > "$targetfile"

    conf=$(curl -s -m 4 "$base/config.json")
    if [ -z "$conf" ]; then
        say "unreachable $host"
        sleep 2
        take_cmd
        case "$CMD" in
            quit) break ;;
            next) IDX=$((IDX + 1)); save_state; continue ;;
            prev) IDX=$((IDX - 1)); save_state; continue ;;
            pageup) turn_page -1; continue ;;
            pagedown) turn_page 1; continue ;;
            rotate) ORIENT=$(((ORIENT + 1) % 4)); save_state; continue ;;
            refresh) continue ;;
        esac
        # Nobody home: the list may be stale, so ask the discovery loop to
        # sweep now rather than waiting out its interval.
        touch "$sweepflag"
        continue
    fi

    rate=$(printf '%s' "$conf" | jq -r '.fps // empty' 2>/dev/null)
    case "$rate" in
        '' | *[!0-9]*) rate=$STREAM_FPS ;;
    esac

    # An older PC build does not report a page count; it also only has one page,
    # so falling back to 1 is what keeps UP and DOWN from asking it for a page
    # it cannot draw.
    pages=$(printf '%s' "$conf" | jq -r '.pages // empty' 2>/dev/null)
    case "$pages" in
        '' | 0 | *[!0-9]*) PAGES=1 ;;
        *) PAGES=$pages ;;
    esac
    [ "$PAGE" -ge "$PAGES" ] && PAGE=0

    # Each PC has its own rotate180, so the compensation is recomputed per PC -
    # otherwise switching between a PC that pre-flips and one that does not
    # turns the picture upside down halfway through a session.
    srv_flip=$(printf '%s' "$conf" | jq -r 'if .rotate180 then 1 else 0 end' \
               2>/dev/null)
    case "$srv_flip" in 0 | 1) ;; *) srv_flip=1 ;; esac
    send=$(compensated_orient "$srv_flip")

    devs=$(device_list)
    say "connect $host idx=$IDX/$total orient=$ORIENT send=$send" \
        "srv_flip=$srv_flip rate=$rate devs=$devs"
    play "$base/stream.mjpg?orient=$send&page=$PAGE&devs=$devs&i=$IDX" "$rate"
    say "mpv exit $?"

    take_cmd
    if [ -z "$CMD" ]; then
        sleep 1
        take_cmd
    fi

    case "$CMD" in
        quit) break ;;
        next) IDX=$((IDX + 1)); save_state ;;
        prev) IDX=$((IDX - 1)); save_state ;;
        pageup) turn_page -1 ;;
        pagedown) turn_page 1 ;;
        rotate) ORIENT=$(((ORIENT + 1) % 4)); save_state ;;
        refresh)
            new=$(grep -n "^$host|" "$hosts" 2>/dev/null | cut -d: -f1 | head -1)
            IDX=$([ -n "$new" ] && echo $((new - 1)) || echo 0)
            save_state
            ;;
    esac
done

say "exit"
cleanup
