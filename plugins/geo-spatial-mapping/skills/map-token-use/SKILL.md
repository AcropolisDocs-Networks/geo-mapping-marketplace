---
name: map-token-use
description: Show where this session's tokens went and what to change, for the geo-spatial-mapping map plugin
disable-model-invocation: true
---
Report token use for this session. Run the session report and give the person the answer in plain words, not the raw output:

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${LOCALAPPDATA:-}/AcropolisDocs/geo-mapping/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/session_report.py" tokens
```

It reads the session transcript on this machine (counts, tool names and script names only; no message or command text is read out; every line is scrubbed of key, token and password patterns first). Say how much was new input, how much came from cache, how much was output, which tools and plugin scripts returned the most, and the one or two changes that would help most. If it says no transcript was found, say so plainly and do not estimate from memory; offer `session_report.py tokens --transcript FILE` if the person has the file. Per-tool figures are measured (1.15.0): each tool result is the growth of the context between one reply and the next, shared among the results that came back together; the few that cannot be measured (a gap with the person's own text, a compacted conversation) are estimated at the session's own measured characters per token. Say once how many were measured, as the report's last line does.
