#!/bin/sh
# Finds a Python 3.11+ that pycheck.py passes and prints its path (nothing when none). pycheck.py also rejects a Python whose own standard library is
# missing and one without the venv module (1.12.3); the reason for each one skipped goes to the file named in PYLOG when the caller sets it. Sourced-free
# and silent otherwise: stdout of the caller belongs to the MCP protocol, so launch.sh reads this through command substitution only.
# Order: newest named version first, then python3, then python.
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
for c in python3.13 python3.12 python3.11 python3 python; do
  p=$(command -v "$c" 2>/dev/null) || continue
  [ -n "$p" ] || continue
  if "$p" "$HERE/pycheck.py" >/dev/null 2>&1; then
    printf '%s\n' "$p"
    exit 0
  fi
  if [ -n "${PYLOG:-}" ]; then "$p" "$HERE/pycheck.py" >>"$PYLOG" 2>/dev/null; fi
done
exit 1
