#!/bin/sh
# PC Monitor - muOS launcher (tested on Anbernic RG35XX Pro, board rg35xx-pro).
#
# All rendering happens on the PC; the handheld only decodes the MJPEG stream
# full-screen. Same workflow as the other two ports: discover PCs on the LAN,
# switch between them, rotate the layout, quit, report battery.
#
# Controls
#     LEFT / RIGHT   prev / next discovered PC
#     UP / DOWN      turn the page (overview / detail)
#     Y              rotate, 4 positions
#     SELECT         quit back to the muOS menu
#
# muOS sits between the Miyoo and ROCKNIX ports rather than matching either:
#
#   * The shell is busybox ash, like the Miyoo - but this busybox is 1.36 and
#     has dropped `timeout -t`, like ROCKNIX. So it is `timeout 2`, and the
#     Miyoo's `timeout -t 2` would exit without ever running the read.
#   * The panel is 640x480, same as the Miyoo, so the frame needs no scaling at
#     all - but it is not mounted upside down, so the PC's 180-degree
#     pre-rotation has to come off. See PANEL_FLIP below.
#   * SDL2 here has only the `mali` video driver and there is no /dev/dri, so
#     rather than gamble on ffplay finding a window system, ffmpeg writes
#     straight to /dev/fb0 with its fbdev muxer. The framebuffer is BGRA
#     (`rgba 8/16,8/8,8/0,8/24`), which is what -pix_fmt bgra produces.
#   * The kernel is 64-bit, so the Miyoo's 16-byte struct input_event read is
#     the wrong size here. js0 is used instead: a js_event is 8 bytes on every
#     architecture, so the same reader works on both this and ROCKNIX.

set -u

progdir=$(cd "$(dirname "$0")" && pwd)

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

FBDEV=/dev/fb0
JSDEV=/dev/input/js0

# Button indices and axis numbers on js0. These were measured on the device by
# pressing each button and reading the index out of the log, not derived, and
# the difference matters: joydev numbers buttons in ascending keycode order, so
# the index follows from the driver's key bitmap - but *which physical button*
# each keycode belongs to is the board's wiring, and muOS-Keys on rg35xx-pro
# does not follow the usual convention. Reading the bitmap predicts Y at 4
# (BTN_WEST) and SELECT at 10 (BTN_SELECT); on the hardware they are 2 and 6.
#
# The ROCKNIX port's numbers differ again - that pad omits BTN_C and BTN_Z, and
# reports its d-pad as four buttons rather than as a hat. There is no portable
# button table. Any unmapped press is logged as `btn N (unbound)`, which is how
# these were found and how to re-find them on another board.
JSBTN_Y=2            # rotate
JSBTN_SELECT=6       # quit
JSAXIS_DPAD_X=4      # ABS_HAT0X: negative is left, positive is right
# ABS_HAT0Y is the companion axis, so it follows ABS_HAT0X. If a device ever
# numbers them otherwise the log prints the axis number of every event, which is
# how the value above was found in the first place.
JSAXIS_DPAD_Y=5      # ABS_HAT0Y: negative is up, positive is down
JSAXIS_THRESHOLD=16384

PC_HOST=192.168.2.114
PC_PORT=8765
STREAM_FPS=8
DISCOVER_EVERY_S=120
BATT_EVERY_S=60
BATT_LOW_PCT=15
BATT_BUZZ_GAP_S=600
SPEAK_EVERY_S=60
RUMBLE_DEV=/sys/class/power_supply/axp2202-battery/moto
BATT_SYS=/sys/class/power_supply/axp2202-battery
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

IDX=0
ORIENT=0
PAGE=0
# How many pages the PC draws. Replaced by whatever /config.json reports
# on each connect; this is only what to assume before the first answer.
PAGES=2
[ -f "$state" ] && . "$state"

: > "$log"
# Everything the shell itself has to say - ffmpeg's "Terminated" whenever the
# button reader stops the player, and any error from the script - belongs in
# our own log rather than in muOS's.
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
# PC to the address it came from.
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

# muOS drives the motor the same way its own RUMBLE helper does: 1 to start,
# 0 to stop. Unlike the ROCKNIX pad, whose force feedback needs an ioctl no
# shell can issue, this one is a plain sysfs file - so the Miyoo's low-battery
# buzz works here too. BATT_LOW_PCT=0 turns it off.
buzz() {
    [ -w "$RUMBLE_DEV" ] || return
    echo 1 > "$RUMBLE_DEV" 2>/dev/null
    sleep 0.4
    echo 0 > "$RUMBLE_DEV" 2>/dev/null
}

battery_loop() {
    last_buzz=0
    while [ ! -f "$stopflag" ]; do
        battery_read
        target=$(cat "$targetfile" 2>/dev/null)
        if [ -n "$BATT_PCT" ] && [ -n "$target" ]; then
            curl -s -m 3 -o /dev/null \
                "$target/battery?pct=$BATT_PCT&charging=$BATT_CHG"
        fi

        if [ -n "$BATT_PCT" ] && [ "$BATT_CHG" = "0" ] &&
           [ "$BATT_LOW_PCT" -gt 0 ] && [ "$BATT_PCT" -le "$BATT_LOW_PCT" ]; then
            now=$(date +%s)
            if [ $((now - last_buzz)) -ge "$BATT_BUZZ_GAP_S" ]; then
                last_buzz=$now
                say "battery $BATT_PCT% - buzz"
                buzz
            fi
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
# The video path here is ffmpeg writing to the framebuffer, which says nothing
# about audio. ffplay is tried first because it needs no output device named on
# the command line; ffmpeg piping to ALSA is the fallback for builds that ship
# the muxers but not the player. If neither works it is logged once and the
# stream carries on - a missing voice must never cost the picture.
speak_play() {
    if command -v ffplay >/dev/null 2>&1; then
        ffplay -hide_banner -loglevel error -nodisp -autoexit -i "$1" \
            >> "$log" 2>&1 && return 0
    fi
    if command -v ffmpeg >/dev/null 2>&1; then
        ffmpeg -hide_banner -loglevel error -nostdin -i "$1" \
            -f alsa default >> "$log" 2>&1 && return 0
    fi
    say "no audio player for advice playback"
    return 1
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
# Writes an action then kills the player, in that order, so the main loop
# always finds the action already waiting when the player returns.
#
# A js_event is 8 bytes: time(4) value(2 LE) type(1) number(1), so `od -tu1 -w8`
# prints one event per line as fields $1..$8 - $7 is the type and $8 the button
# or axis. `-v` is not optional: without it od collapses a line identical to the
# one before into a bare `*`, and two presses of one button in the same second
# produce exactly that.
#
# The read is wrapped in a 2-second timeout instead of blocking forever so the
# loop can notice the stop flag and exit on its own. A blocked `dd` orphaned by
# a kill would keep its own copy of the js0 queue open, and the next launch
# would then lose half its events to the leftover reader.
keyreader() {
    exec 3< "$JSDEV"
    # busybox ash announces every child that dies on a signal, and the 2-second
    # read timeout kills one `dd` per cycle for as long as the app is open -
    # which is four lines of "Terminated" every eight seconds, drowning the log
    # in the one place a misbound button would show up. Nothing else in this
    # loop writes to stderr, and `say` writes to the log directly.
    exec 2>/dev/null
    while [ ! -f "$stopflag" ]; do
        ev=$(timeout 2 dd bs=8 count=1 2>/dev/null <&3 | od -An -tu1 -w8 -v)
        [ -z "$ev" ] && continue
        set -- $ev
        [ "$#" -eq 8 ] || continue

        val=$((($6 << 8) | $5))
        [ "$val" -gt 32767 ] && val=$((val - 65536))
        act=""

        case "$7" in
            1)  # JS_EVENT_BUTTON; the 0x80 bit marks the driver's initial burst
                [ "$val" = "1" ] || continue        # press, not release
                case "$8" in
                    "$JSBTN_Y") act=rotate ;;
                    "$JSBTN_SELECT") act=quit ;;
                    *) say "btn $8 (unbound)"; continue ;;
                esac
                ;;
            2)  # JS_EVENT_AXIS - the d-pad is a hat on this device
                case "$8" in
                    "$JSAXIS_DPAD_X") neg=prev; pos=next ;;
                    "$JSAXIS_DPAD_Y") neg=pageup; pos=pagedown ;;
                    *) continue ;;
                esac
                if [ "$val" -le "-$JSAXIS_THRESHOLD" ]; then
                    act=$neg
                elif [ "$val" -ge "$JSAXIS_THRESHOLD" ]; then
                    act=$pos
                else
                    continue                        # returned to centre
                fi
                ;;
            *) continue ;;
        esac

        echo "$act" > "$cmdfile"
        say "js type=$7 num=$8 val=$val -> $act"
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
# The frame is 640x480 and so is the panel, so nothing scales: ffmpeg decodes
# each JPEG and writes it to the framebuffer as-is. -nostdin matters because
# ffmpeg otherwise reads the terminal looking for 'q', and this runs with the
# launcher's stdin attached.
play() {
    ffmpeg -hide_banner -loglevel error -nostdin \
        -f mjpeg -framerate "$2" -fflags nobuffer -flags low_delay \
        -i "$1" -an -sn -pix_fmt bgra -f fbdev "$FBDEV" >> "$log" 2>&1 &
    PLAYER=$!
    echo "$PLAYER" > "$pidfile"
    wait "$PLAYER"
    rc=$?
    : > "$pidfile"
    return $rc
}

# --- stream watchdog -------------------------------------------------------
# The player does not reliably notice a stream that died under it: when the
# PC's server exits, or WiFi drops, the socket goes away but it can sit on a
# frozen frame forever - and the main loop is blocked in `wait`, so nothing
# reconnects until the user quits.
#
# The kernel knows the truth, so look for an established connection to the
# PC's port in /proc/net/tcp and kill the player when there has been none for
# two checks running. Two checks rather than one because the connection is
# legitimately absent for an instant between reconnects, and one check may also
# catch the battery reporter's own short-lived connection.
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
    echo 0 > "$RUMBLE_DEV" 2>/dev/null
}
trap 'cleanup; exit 0' INT TERM

# A previous run that was killed rather than exited would otherwise keep
# reading the input device and writing to the framebuffer.
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

say "start idx=$IDX orient=$ORIENT page=$PAGE panel_flip=$PANEL_FLIP"

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
    say "player exit $?"

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
