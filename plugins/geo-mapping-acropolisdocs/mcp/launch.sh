#!/bin/sh
# Starts the bundled OGC MCP server on macOS and Linux (launch.cmd is the Windows twin and behaves the same way). Runs it from the private
# environment that setup.sh builds; if that is not ready it uses a system Python that already has the server's packages, and otherwise
# starts setup.sh once in the background (log: setup.log beside the environment) and asks for a restart.
# Stdout belongs to the MCP protocol, so every check here is silent and every message goes to stderr.
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
ROOT=$(dirname -- "$HERE")
. "$HERE/paths.sh"
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*) echo "geo-mapping: on Windows the server starts through launch.cmd, not launch.sh." >&2; exit 1 ;;
esac
unset PYTHONHOME PYTHONPATH        # another program's Python settings would point this one at the wrong standard library
STARTED=0

running() {                          # a setup that is really still going: its marker names a live process
  [ -f "$BASE/setup.running" ] || return 1
  pid=$(sed -n 's/^started \([0-9][0-9]*\)$/\1/p' "$BASE/setup.running" 2>/dev/null)
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && return 0
  rm -f "$BASE/setup.running"        # left behind by an interrupted install
  return 1
}

start_setup() {
  mkdir -p "$BASE" 2>/dev/null || return 1
  nohup sh "$HERE/setup.sh" all >"$BASE/setup.log" 2>&1 </dev/null &
  echo "started $!" > "$BASE/setup.running"      # at once, so a second start in the same second sees it (setup.sh writes the same line)
  STARTED=1
  echo "geo-mapping: installing the map packages, the optional extras and Chromium in the background (once). Log: $BASE/setup.log" >&2
}

# 1.12.3: launch.log (the latest start only) says what this start decided and why, so a setup that did not start leaves a reason.
mkdir -p "$BASE" 2>/dev/null
LOG="$BASE/launch.log"
echo "$(date) launch.sh from $HERE" > "$LOG" 2>/dev/null
BADVENV=0
[ -x "$VENVPY" ] && ! "$VENVPY" "$HERE/pycheck.py" >/dev/null 2>&1 && BADVENV=1    # built on a Python that is not whole: setup rebuilds it

# The first time, and when a new version changes a package list, install everything by itself; the server starts meanwhile from whatever
# Python already works. Never twice at once, and not again after a failure (setup.failed says to run setup.sh by hand).
if running; then
  echo "not starting setup: it is already running" >> "$LOG"
elif [ -f "$BASE/setup.failed" ]; then
  echo "not starting setup: setup.failed exists (run setup.sh by hand to see why)" >> "$LOG"
elif [ "$BADVENV" = 1 ] || ! cmp -s "$HERE/requirements.txt" "$STAMP" || ! cmp -s "$ROOT/requirements.txt" "$FULLSTAMP"; then
  echo "starting setup (broken environment: $BADVENV; otherwise the package lists changed or the full install never finished)" >> "$LOG"
  start_setup
fi

if [ "$BADVENV" = 0 ] && [ -x "$VENVPY" ] && cmp -s "$HERE/requirements.txt" "$STAMP" && "$VENVPY" -c "$IMPORTS" >/dev/null 2>&1; then
  exec "$VENVPY" "$HERE/ogc_server.py" "$@"
fi

PYLOG="$LOG"
export PYLOG
PY=$(sh "$HERE/findpython.sh") || {
  echo "geo-mapping: no Python 3.11 or newer was found. Install one (python.org, Homebrew or your package manager) and restart the plugin." >&2
  exit 1
}
if "$PY" -c "$IMPORTS" >/dev/null 2>&1; then
  exec "$PY" "$HERE/ogc_server.py" "$@"
fi
if running; then
  echo "geo-mapping: the package install is still running (log: $BASE/setup.log). Restart the plugin when it says Done." >&2
  exit 1
fi
if [ -f "$BASE/setup.failed" ]; then
  echo "geo-mapping: the package install failed earlier. Run  sh \"$HERE/setup.sh\"  to see why, then restart the plugin." >&2
  exit 1
fi
[ "$STARTED" = 1 ] || start_setup
echo "geo-mapping: first run. Installing the Python packages in the background; restart the plugin in a few minutes (log: $BASE/setup.log)." >&2
exit 1
