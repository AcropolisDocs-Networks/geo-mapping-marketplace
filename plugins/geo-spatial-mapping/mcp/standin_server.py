"""4.0.0 (spec B6): the data server's stand-in while its packages are not ready. Standard library only, so any Python 3.8 or newer runs it.

launch.cmd / launch.sh start it instead of exiting when the private environment is not ready (installing, failed, never installed). It
speaks the MCP protocol over stdio (one JSON-RPC message a line), so the connection does not fail and Claude does not cache a failure for
15 minutes, and it serves one tool, `setup_status`: the state, the cause code and its fix, and the last lines of the setup log. When
setup has finished it says to restart Claude to load the map tools. It never hands over to the real server in the same session (D6).

    <python> standin_server.py [--base ENV_FOLDER]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import setup_env as se  # noqa: E402

NAME = "geo-spatial-mapping-ogc"
TOOL = {
    "name": "setup_status",
    "description": ("The map plugin's data server is not ready on this computer yet (its Python packages are installing, or the install "
                    "failed). This tool says which, the cause and the one-line fix, and the last lines of the setup log. Tell the person the "
                    "fix in plain words; after setup says Done, Claude must be restarted to load the map tools."),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}
INSTRUCTIONS = ("The map plugin's data server is starting in setup mode: its packages are not ready on this computer, so its map and data tools "
                "are not available in this session. Call setup_status to learn why and tell the person the fix; once setup says Done, the "
                "person restarts Claude and the tools appear. Map building in the session's shell does not need this server.")


def version() -> str:
    try:
        return (HERE.parent / ".claude-plugin" / "plugin.json").is_file() and json.loads(
            (HERE.parent / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")).get("version") or "?"
    except (OSError, ValueError):
        return "?"


def handle(msg: dict, base: Path) -> dict | None:
    """The answer to one message, or None for a notification."""
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}
    if mid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18", "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": NAME, "version": version() + " (setup)"}, "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        if params.get("name") != "setup_status":
            result = {"content": [{"type": "text", "text": json.dumps({"ok": False, "state": "setup", "message":
                      "Only setup_status is available until the data server's packages are installed and Claude is restarted."})}],
                      "isError": True}
        else:
            st = se.status(base, HERE.parent)
            se.server_log(base, f"stand-in setup_status: {st.get('state')} {st.get('cause') or ''}")
            result = {"content": [{"type": "text", "text": json.dumps({"ok": True, **st}, indent=1)}], "isError": False}
    elif method in ("resources/list", "prompts/list"):
        result = {method.split("/")[0]: []}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=None, stdout=None, base: Path | None = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    base = Path(base or se.base_dir())
    se.server_log(base, "stand-in server started (the packages are not ready)")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
            stdout.flush()
            continue
        for m in (msg if isinstance(msg, list) else [msg]):
            ans = handle(m, base) if isinstance(m, dict) else None
            if ans is not None:
                stdout.write(json.dumps(ans) + "\n")
                stdout.flush()
    se.server_log(base, "stand-in server stopped")
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    b = a[a.index("--base") + 1] if "--base" in a and a.index("--base") + 1 < len(a) else None
    sys.exit(serve(base=Path(b) if b else None))
