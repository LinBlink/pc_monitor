#!/bin/sh
# PC Monitor - show a live PC telemetry dashboard streamed over WiFi.
#
# All rendering happens on the PC; the handheld only decodes a 640x480 MJPEG
# stream full-screen, which is well within a Cortex-A7's budget.
#
# Controls:  LEFT / RIGHT  switch between discovered PCs (listed on screen)
#            Y             rotate the display (4 steps, portrait included)
#            MENU          quit
#
# The handheld's own battery level is reported to the PC so it can be drawn in the
# header, and a low battery buzzes the motor.
#
# Buttons are read straight from /dev/input/event0. evdev delivers events to
# every reader, so this works alongside ffplay's own SDL input. The device is
# opened once and held on fd 3 for the whole session: re-opening it per read
# silently drops any event that arrived in between.
#
# The frame rate and the orientation both live on the PC side — the rate is read
# from /config.json on each connect, and the orientation and the discovered device
# list are passed as query parameters so the PC renders the matching layout and
# switcher. Nothing here needs to know how the dashboard is drawn.

progdir=$(dirname "$0")
sysdir=/mnt/SDCARD/.tmp_update
BIN=$sysdir/bin
log="$progdir/pcmonitor.log"
hosts="$progdir/hosts.txt"
state="$progdir/state.cfg"
cmdfile=/tmp/pcmon_cmd
stopflag=/tmp/pcmon_stop
pidfile=/tmp/pcmon_player
targetfile=/tmp/pcmon_target
selfpid="$progdir/.pid"

# evdev key codes on the Miyoo Mini.
KEY_MENU=1
KEY_LEFT=105
KEY_RIGHT=106
KEY_Y=56

PC_HOST=192.168.2.114
PC_PORT=8765
STREAM_FPS=8
BATT_EVERY_S=60
BATT_LOW_PCT=15
BATT_BUZZ_GAP_S=600
[ -f "$progdir/settings.cfg" ] && . "$progdir/settings.cfg"

IDX=0
ORIENT=0
[ -f "$state" ] && . "$state"

export LD_LIBRARY_PATH="$sysdir/lib:/mnt/SDCARD/miyoo/lib:$LD_LIBRARY_PATH"
export SDL_AUDIODRIVER=dummy
export HOME=/mnt/SDCARD

: > "$log"
: > "$cmdfile"
rm -f "$stopflag"

say() { echo "$(date '+%H:%M:%S') $*" >> "$log"; }

save_state() { printf 'IDX=%s\nORIENT=%s\n' "$IDX" "$ORIENT" > "$state"; }

host_count() {
	c=$(wc -l < "$hosts" 2>/dev/null)
	[ -z "$c" ] && c=0
	echo "$c"
}

# The PC draws the device switcher, but only the handheld knows what is on the
# LAN, so the names ride along in the stream URL. Anything outside the unreserved
# URL characters is dropped rather than escaped — these are hostnames, and a name
# that survives is worth more than a faithful one.
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
# No UDP here: this busybox nc has no -u, and ping has no -b, so broadcast and
# multicast are both off the table. A TCP connect sweep of the /24 with 48 in
# flight covers the subnet in about five seconds, which is fast enough to run in
# the background while the remembered host is already on screen.
discover() {
	myip=$(/sbin/ifconfig wlan0 2>/dev/null |
		sed -n 's/.*inet addr:\([0-9.]*\).*/\1/p' | head -1)
	if [ -z "$myip" ]; then
		say "discover: no wlan0 address"
		return
	fi
	net=$(echo "$myip" | cut -d. -f1-3)
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
		name=$("$BIN/curl" -s -m 3 "http://$ip:$PC_PORT/config.json" |
			"$BIN/jq" -r '.name // empty' 2>/dev/null)
		if [ -n "$name" ]; then
			echo "$ip|$name" >> "$found"
		else
			say "discover: $ip open but not a PC Monitor"
		fi
	done

	if [ -s "$found" ]; then
		if cmp -s "$found" "$hosts"; then
			say "discover: found $(host_count) device(s), unchanged"
		else
			cp -f "$found" "$hosts"
			say "discover: found $(host_count) device(s): $(tr '\n' ' ' < "$hosts")"
			# The switcher is drawn from the list the URL carried, so a changed
			# list means the frame on screen is stale: ask for a reconnect.
			echo refresh > "$cmdfile"
			[ -s "$pidfile" ] && kill -9 "$(cat "$pidfile")" 2>/dev/null
		fi
	else
		say "discover: nothing found, keeping $(host_count) known"
	fi
}

# --- battery ---------------------------------------------------------------
# The dashboard is drawn on the PC, so the handheld's own charge level has to be
# sent there: /battery?pct=..&charging=.. once a minute, attributed by the PC to
# the address it came from.
#
# /customer/app/axp_test talks to the PMIC and prints
#   {"battery":100, "voltage":4155, "charging":3}
# It has to run from its own directory. charging=3 is "plugged in" on this
# firmware. /tmp/percBat (kept fresh by Onion's batmon) is the fallback for the
# percentage, but it says nothing about charging.
battery_read() {
	BATT_PCT=""
	BATT_CHG=0
	raw=$(cd /customer/app 2>/dev/null && ./axp_test 2>/dev/null)
	if [ -n "$raw" ]; then
		BATT_PCT=$(echo "$raw" | sed -n 's/.*"battery": *\([0-9]*\).*/\1/p')
		chg=$(echo "$raw" | sed -n 's/.*"charging": *\([0-9]*\).*/\1/p')
		[ "$chg" = "3" ] && BATT_CHG=1
	fi
	if [ -z "$BATT_PCT" ] && [ -f /tmp/percBat ]; then
		BATT_PCT=$(cat /tmp/percBat 2>/dev/null)
	fi
	case "$BATT_PCT" in
		'' | *[!0-9]*) BATT_PCT="" ;;
	esac
}

# The motor hangs off gpio48, which rests high — the same pin Onion's keymon
# pulses. Onion's own opt-out is honoured so this never overrides the user's
# setting for the whole device.
vibrate() {
	[ -f "$sysdir/config/.noVibration" ] && return
	[ -e /sys/class/gpio/gpio48/value ] || return
	echo out > /sys/class/gpio/gpio48/direction 2>/dev/null
	echo 0 > /sys/class/gpio/gpio48/value 2>/dev/null
	usleep 200000 2>/dev/null || sleep 1
	echo 1 > /sys/class/gpio/gpio48/value 2>/dev/null
}

battery_loop() {
	LAST_BUZZ=0
	while [ ! -f "$stopflag" ]; do
		battery_read
		target=$(cat "$targetfile" 2>/dev/null)
		if [ -n "$BATT_PCT" ] && [ -n "$target" ]; then
			"$BIN/curl" -s -m 3 -o /dev/null \
				"$target/battery?pct=$BATT_PCT&charging=$BATT_CHG"
		fi

		if [ -n "$BATT_PCT" ] && [ "$BATT_LOW_PCT" -gt 0 ] &&
			[ "$BATT_CHG" = "0" ] && [ "$BATT_PCT" -le "$BATT_LOW_PCT" ]; then
			now=$(date +%s)
			if [ $((now - LAST_BUZZ)) -ge "$BATT_BUZZ_GAP_S" ]; then
				LAST_BUZZ=$now
				say "battery $BATT_PCT% low, buzzing"
				vibrate
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

# --- button reader ---------------------------------------------------------
# Writes an action then kills ffplay, in that order, so the main loop always
# finds the action already waiting when ffplay returns.
keyreader() {
	exec 3< /dev/input/event0
	while [ ! -f "$stopflag" ]; do
		# The 2s cap lets the loop notice the stop flag; fd 3 stays open across
		# it, so the kernel keeps queueing events and none are missed.
		ev=$(timeout -t 2 dd bs=16 count=1 2>/dev/null <&3 | od -An -tu2)
		[ -z "$ev" ] && continue
		set -- $ev
		# od -tu2 fields: 1-4 timeval, 5 type, 6 code, 7 value low half.
		[ "$5" = "1" ] || continue
		[ "$7" = "1" ] || continue
		case "$6" in
			"$KEY_LEFT") echo prev > "$cmdfile" ;;
			"$KEY_RIGHT") echo next > "$cmdfile" ;;
			"$KEY_Y") echo rotate > "$cmdfile" ;;
			"$KEY_MENU") echo quit > "$cmdfile" ;;
			*)
				say "key $6 unbound"
				continue
				;;
		esac
		# Kill the recorded player rather than matching on a process name, so
		# nothing else named ffplay can be caught by accident.
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

# Runs the player in the background and records its pid so the button reader can
# stop exactly this process, then waits for it and returns its status.
# PCMON_TEST_PLAY is a hook for exercising the loop without a framebuffer.
play() {
	if [ -n "$PCMON_TEST_PLAY" ]; then
		( eval "$PCMON_TEST_PLAY" ) &
	else
		"$BIN/ffplay" -hide_banner -loglevel warning -autoexit \
			-f mjpeg -framerate "$2" \
			-fflags nobuffer -flags low_delay -framedrop \
			-an -sn -x 640 -y 480 -i "$1" >> "$log" 2>&1 &
	fi
	PLAYER=$!
	echo "$PLAYER" > "$pidfile"
	wait "$PLAYER"
	rc=$?
	: > "$pidfile"
	return $rc
}

cleanup() {
	touch "$stopflag"
	rm -f /tmp/stay_awake "$selfpid"
	[ -s "$pidfile" ] && kill -9 "$(cat "$pidfile")" 2>/dev/null
	[ -n "$KR_PID" ] && kill "$KR_PID" 2>/dev/null
	[ -n "$DISC_PID" ] && kill "$DISC_PID" 2>/dev/null
	[ -n "$BATT_PID" ] && kill "$BATT_PID" 2>/dev/null
	rm -f "$targetfile"
	echo ondemand > /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null
}
trap 'cleanup; exit 0' INT TERM

# A previous run that was killed rather than exited would otherwise keep reading
# the input device and fighting over the framebuffer.
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

echo performance > /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null
touch /tmp/stay_awake

# Seed the list with the configured host so the first frame does not wait for a
# subnet sweep; discovery then refines it in the background.
[ -s "$hosts" ] || echo "$PC_HOST|$PC_HOST" > "$hosts"

keyreader &
KR_PID=$!
discover &
DISC_PID=$!
battery_loop &
BATT_PID=$!

say "start idx=$IDX orient=$ORIENT"

while :; do
	total=$(host_count)
	[ "$total" -lt 1 ] && total=1
	[ "$IDX" -ge "$total" ] && IDX=0
	[ "$IDX" -lt 0 ] && IDX=$((total - 1))

	line=$(sed -n "$((IDX + 1))p" "$hosts" 2>/dev/null)
	host=$(echo "$line" | cut -d'|' -f1)
	[ -z "$host" ] && host=$PC_HOST
	base="http://$host:$PC_PORT"
	# The battery reporter posts to whichever PC is currently on screen.
	echo "$base" > "$targetfile"

	conf=$("$BIN/curl" -s -m 4 "$base/config.json")
	if [ -z "$conf" ]; then
		say "unreachable $host"
		sleep 2
		take_cmd
		case "$CMD" in
			quit) break ;;
			next) IDX=$((IDX + 1)); save_state; continue ;;
			prev) IDX=$((IDX - 1)); save_state; continue ;;
			rotate) ORIENT=$(((ORIENT + 1) % 4)); save_state; continue ;;
			# A fresh list may well contain a host that is actually up.
			refresh) continue ;;
		esac
		# Nothing pressed and nobody home: the list may be stale, so sweep again
		# unless a sweep is already running.
		if ! kill -0 "$DISC_PID" 2>/dev/null; then
			discover &
			DISC_PID=$!
		fi
		continue
	fi

	rate=$(printf '%s' "$conf" | "$BIN/jq" -r '.fps // empty' 2>/dev/null)
	case "$rate" in
		'' | *[!0-9]*) rate=$STREAM_FPS ;;
	esac

	devs=$(device_list)
	say "connect $host idx=$IDX/$total orient=$ORIENT rate=$rate devs=$devs"
	play "$base/stream.mjpg?orient=$ORIENT&devs=$devs&i=$IDX" "$rate"
	say "ffplay exit $?"

	take_cmd
	if [ -z "$CMD" ]; then
		# ffplay can quit on its own from the MENU key before the reader has
		# processed the same event; give it a moment before treating this as a
		# dropped connection.
		sleep 1
		take_cmd
	fi

	case "$CMD" in
		quit) break ;;
		next) IDX=$((IDX + 1)); save_state ;;
		prev) IDX=$((IDX - 1)); save_state ;;
		rotate) ORIENT=$(((ORIENT + 1) % 4)); save_state ;;
		refresh)
			# Discovery rewrote the list; stay on this PC if it is still in it,
			# otherwise fall back to the first entry.
			new=$(grep -n "^$host|" "$hosts" 2>/dev/null | cut -d: -f1 | head -1)
			IDX=$([ -n "$new" ] && echo $((new - 1)) || echo 0)
			save_state
			;;
	esac
done

say "exit"
cleanup
