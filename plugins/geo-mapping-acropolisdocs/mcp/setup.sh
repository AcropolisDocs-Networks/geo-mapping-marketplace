#!/bin/sh
# macOS and Linux twin of setup.cmd: installs the Python packages the bundled OGC server needs into a private virtual environment (not
# your system Python), once. The environment lives outside the plugin, so it survives plugin updates and is rebuilt only when
# mcp/requirements.txt changes.
#   setup.sh        the server's packages (mcp/requirements.txt)
#   setup.sh all    also the full map-building set (requirements.txt), the optional extras (one at a time) and Playwright's Chromium
# Everything it prints goes to stderr or the log, never stdout (launch.sh starts it while the MCP protocol owns stdout).
exec 1>&2
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
ROOT=$(dirname -- "$HERE")
. "$HERE/paths.sh"
ALL=0
[ "${1:-}" = "all" ] && ALL=1
unset PYTHONHOME PYTHONPATH
mkdir -p "$BASE" || { echo "cannot create $BASE"; exit 1; }
rm -f "$BASE/setup.failed"
echo "started $$" > "$BASE/setup.running"

fail() {
  rm -f "$BASE/setup.running"
  echo failed > "$BASE/setup.failed"
  echo
  echo "Setup did not finish. Fix the message above, then run this file again:  sh \"$HERE/setup.sh\""
  exit 1
}

PY=$(sh "$HERE/findpython.sh") || { echo "No Python 3.11 or newer was found. Install one (python.org, Homebrew or your package manager), then run this again."; fail; }
echo "Using Python: $PY"
if [ ! -x "$VENVPY" ]; then
  echo "Creating the private environment in $VENV"
  "$PY" -m venv "$VENV" || { echo "python -m venv failed. On Debian and Ubuntu install the python3-venv package first."; fail; }
fi
echo "Installing packages. The first run downloads about a hundred megabytes and can take a few minutes."
"$VENVPY" -m pip install --upgrade --disable-pip-version-check --progress-bar off pip || fail
"$VENVPY" -m pip install --disable-pip-version-check --progress-bar off -r "$HERE/requirements.txt" || fail
"$VENVPY" -c "$IMPORTS" >/dev/null 2>&1 || { echo "The install finished but the packages do not import. Run:  \"$VENVPY\" -m pip check"; fail; }
cp "$HERE/requirements.txt" "$STAMP"

if [ "$ALL" = 1 ]; then
  echo "Installing the full map-building set."
  if [ -f "$ROOT/constraints.txt" ] && "$VENVPY" -m pip install --disable-pip-version-check --progress-bar off -r "$ROOT/requirements.txt" -c "$ROOT/constraints.txt"; then
    :
  else
    [ -f "$ROOT/constraints.txt" ] && echo "The tested versions could not all be installed here; installing the newest that fit instead."
    "$VENVPY" -m pip install --disable-pip-version-check --progress-bar off -r "$ROOT/requirements.txt" || fail
  fi
  echo "Installing the optional extras and Playwright's Chromium."
  "$VENVPY" "$HERE/install_extras.py" "$ROOT/requirements-optional.txt"
  cp "$ROOT/requirements.txt" "$FULLSTAMP"
  "$VENVPY" -c "import weasyprint" >/dev/null 2>&1 || {
    echo
    echo "NOTE: WeasyPrint installed but cannot load its native libraries, so PDF export will not work from this environment."
    echo "      It needs Pango (macOS: brew install pango; Debian/Ubuntu: apt install libpango-1.0-0 libpangoft2-1.0-0). SVG and PNG do not need it."
  }
fi
rm -f "$BASE/setup.running"
echo
echo "Done. Restart the plugin so the OGC server picks up the packages."
echo "The environment is at $VENV"
exit 0
