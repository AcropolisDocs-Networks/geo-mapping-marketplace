#!/bin/sh
# macOS and Linux twin of setup.cmd: installs the Python packages the bundled data server needs into a private virtual environment (not
# your system Python), once. The environment lives outside the plugin, so it survives plugin updates and is rebuilt only when
# mcp/requirements.txt changes. 4.0.0: this file finds a Python and hands over to setup_env.py (the checks, the install, setup.log beside the
# environment, and a cause code with a one-line fix when it fails).
#   setup.sh          the server's packages (mcp/requirements.txt)
#   setup.sh all      also the full map-building set (requirements.txt), the optional extras (one at a time) and Playwright's Chromium
#   setup.sh /repair  removes the environment and its markers, then installs again
# Everything it prints goes to stderr or the log, never stdout (launch.sh starts it while the MCP protocol owns stdout).
exec 1>&2
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
ROOT=$(dirname -- "$HERE")
. "$HERE/paths.sh"
unset PYTHONHOME PYTHONPATH
mkdir -p "$BASE" || { echo "cannot create $BASE"; exit 1; }
PYLOG="$BASE/python-check.log"
export PYLOG
rm -f "$PYLOG"
PY=$(sh "$HERE/findpython.sh") || {
  {
    echo "NO_PYTHON: no usable Python 3.11 or newer was found."
    echo "Fix: install Python 3.13 (python.org, Homebrew or your package manager), then run setup again."
    [ -s "$PYLOG" ] && { echo "Pythons not used:"; cat "$PYLOG"; }
  } > "$BASE/setup.log"
  printf 'NO_PYTHON\ninstall Python 3.13, then run setup again\n' > "$BASE/setup.failed"
  rm -f "$BASE/setup.running"
  cat "$BASE/setup.log"
  echo
  echo "Log: $BASE/setup.log"
  exit 1
}
exec "$PY" "$HERE/setup_env.py" --root "$ROOT" --base "$BASE" --pylog "$PYLOG" "$@"
