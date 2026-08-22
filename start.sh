#!/bin/sh
# Start the PC Monitor stream server in the foreground (prints the URL).
# The Linux counterpart of start.bat; for a machine that should keep serving
# after you log out, use `python3 server.py --install-autostart` instead.
cd "$(dirname "$0")" || exit 1
exec python3 server.py "$@"
