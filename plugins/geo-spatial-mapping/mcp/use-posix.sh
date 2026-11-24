#!/bin/sh
# macOS and Linux: point the plugin's server entry at launch.sh (the shipped .mcp.json names the Windows launcher, because a plugin's server
# entry cannot yet differ by operating system). Run once from anywhere, and again after a plugin update:   sh mcp/use-posix.sh
#   sh mcp/use-posix.sh --windows   puts the Windows entry back      sh mcp/use-posix.sh --check   says which one is set (exit 0 posix, 3 windows)
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
ROOT=$(dirname -- "$HERE")
F="$ROOT/.mcp.json"
[ -f "$F" ] || { echo "no .mcp.json in $ROOT" >&2; exit 1; }
NAME=$(sed -n 's/^[[:space:]]*"\([A-Za-z0-9_.-]*\)":[[:space:]]*{[[:space:]]*$/\1/p' "$F" | sed -n '2p')
[ -n "$NAME" ] || NAME=$(sed -n 's/.*"mcp_server_name":[[:space:]]*"\([^"]*\)".*/\1/p' "$ROOT/BUILD-MANIFEST.json" | head -1)
[ -n "$NAME" ] || { echo "could not read the server's name from $F" >&2; exit 1; }
case "${1:-}" in
  --check)
    if grep -q 'launch\.sh' "$F"; then echo "posix (launch.sh)"; exit 0; fi
    echo "windows (launch.cmd)"; exit 3 ;;
  --windows)
    printf '{\n  "mcpServers": {\n    "%s": {\n      "command": "cmd",\n      "args": ["/c", "${CLAUDE_PLUGIN_ROOT}\\\\mcp\\\\launch.cmd"]\n    }\n  }\n}\n' "$NAME" > "$F" || exit 1
    echo "The server entry starts mcp\\launch.cmd (Windows). Restart the plugin." ;;
  "")
    [ -w "$F" ] || { echo "$F is not writable from here (in a desktop session the plugin's folder is on your own computer: run this there)" >&2; exit 1; }
    printf '{\n  "mcpServers": {\n    "%s": {\n      "command": "/bin/sh",\n      "args": ["${CLAUDE_PLUGIN_ROOT}/mcp/launch.sh"]\n    }\n  }\n}\n' "$NAME" > "$F" || exit 1
    chmod +x "$HERE/launch.sh" "$HERE/setup.sh" "$HERE/findpython.sh" 2>/dev/null
    echo "The server entry starts mcp/launch.sh. Restart the plugin; the first start installs the packages in the background (a few minutes)." ;;
  *) echo "usage: sh use-posix.sh [--windows | --check]" >&2; exit 2 ;;
esac
