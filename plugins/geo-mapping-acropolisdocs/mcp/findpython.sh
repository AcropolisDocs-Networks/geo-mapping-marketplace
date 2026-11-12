#!/bin/sh
# Finds a Python 3.11+ and prints its path (nothing when none). Sourced-free and silent otherwise: stdout of the caller belongs to the
# MCP protocol, so launch.sh reads this through command substitution only. Order: newest named version first, then python3, then python.
for c in python3.13 python3.12 python3.11 python3 python; do
  p=$(command -v "$c" 2>/dev/null) || continue
  [ -n "$p" ] || continue
  "$p" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1 || continue
  printf '%s\n' "$p"
  exit 0
done
exit 1
