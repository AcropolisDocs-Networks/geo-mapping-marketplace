---
name: map-token-use
description: Show where this session's tokens went and what to change, for the geo-mapping-acropolisdocs map plugin
disable-model-invocation: true
---
Report token use for this session. Run the session report and give the person the answer in plain words, not the raw output:

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/session_report.py" tokens
```

It reads the session transcript on this machine (counts, tool names and script names only; no message or command text is read out; every line is scrubbed of key, token and password patterns first). Say how much was new input, how much came from cache, how much was output, which tools and plugin scripts returned the most, and the one or two changes that would help most. If it says no transcript was found, say so plainly and do not estimate from memory; offer `session_report.py tokens --transcript FILE` if the person has the file. Tool-output figures are estimates (characters / 4); say so once.
