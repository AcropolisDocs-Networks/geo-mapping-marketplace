#!/bin/sh
# Starts the bundled OGC MCP server on macOS and Linux (launch.cmd is the Windows twin and behaves the same way). Runs it from the private
# environment that setup.sh builds; if that is not ready it uses a system Python that already has the server's packages, and otherwise
# starts setup.sh once in the background (log: setup.log beside the environment) and asks for a restart.
# Stdout belongs to the MCP protocol, so every check here is silent and every message goes to stderr.
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
ROOT=$(dirname -- "$HERE")
. "$HERE/paths.sh"
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*) echo "geo-spatial-mapping: on Windows the server starts through launch.cmd, not launch.sh." >&2; exit 1 ;;
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
  nohup sh "$HERE/setup.sh" all >"$BASE/setup.out" 2>&1 </dev/null &
  echo "started $!" > "$BASE/setup.running"      # at once, so a second start in the same second sees it (setup.sh writes the same line)
  STARTED=1
  echo "geo-spatial-mapping: installing the map packages, the optional extras and Chromium in the background (once). Log: $BASE/setup.log" >&2
}

# 1.12.3: launch.log (the latest start only) says what this start decided and why, so a setup that did not start leaves a reason.
mkdir -p "$BASE" 2>/dev/null
LOG="$BASE/launch.log"
echo "$(date) launch.sh from $HERE" > "$LOG" 2>/dev/null
BADVENV=0
[ -x "$VENVPY" ] && ! "$VENVPY" "$HERE/pycheck.py" >/dev/null 2>&1 && BADVENV=1    # built on a Python that is not whole: setup rebuilds it

# 4.0.0 (spec B8): the server's own set ready means start the server, and no setup is started under it (a setup that changes the environment
# a running server has loaded can crash it); the full set waits for setup.sh all with Claude closed. Not ready: setup starts by itself, and
# a stand-in server (standin_server.py, standard library only) answers setup_status meanwhile, so the connection does not fail.
if [ "$BADVENV" = 0 ] && [ -x "$VENVPY" ] && cmp -s "$HERE/requirements.txt" "$STAMP" && "$VENVPY" -c "$IMPORTS" >/dev/null 2>&1; then
  cmp -s "$ROOT/requirements.txt" "$FULLSTAMP" || echo "the full map-building set is not current; not started while the server runs (setup.sh all, with Claude closed)" >> "$LOG"
  exec "$VENVPY" "$HERE/ogc_server.py" "$@"
fi

PYLOG="$LOG"
export PYLOG
PY=$(sh "$HERE/findpython.sh") || PY=""
if [ -n "$PY" ]; then
  if grep -q '^NO_PYTHON' "$BASE/setup.failed" 2>/dev/null; then rm -f "$BASE/setup.failed"; fi
  if "$PY" -c "$IMPORTS" >/dev/null 2>&1; then
    exec "$PY" "$HERE/ogc_server.py" "$@"
  fi
  if running; then
    echo "not starting setup: it is already running" >> "$LOG"
  elif [ -f "$BASE/setup.failed" ]; then
    echo "not starting setup: setup.failed exists (setup_status says why; setup.sh /repair installs again)" >> "$LOG"
  else
    start_setup
  fi
  exec "$PY" "$HERE/standin_server.py" --base "$BASE"
fi
[ -f "$BASE/setup.failed" ] || printf 'NO_PYTHON\ninstall Python 3.13, then restart Claude\n' > "$BASE/setup.failed"
ANYPY=$(command -v python3 2>/dev/null || command -v python 2>/dev/null)
if [ -n "$ANYPY" ] && "$ANYPY" -c "import sys; sys.exit(sys.version_info < (3, 8))" >/dev/null 2>&1; then
  exec "$ANYPY" "$HERE/standin_server.py" --base "$BASE"
fi
echo "geo-spatial-mapping: no Python 3.11 or newer was found. Install one (python.org, Homebrew or your package manager) and restart Claude." >&2
exit 1
