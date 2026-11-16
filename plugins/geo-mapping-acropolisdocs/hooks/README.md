# Hooks (on since 1.17.0; the timing hook since 1.17.2)

`hooks.json` turns on the three hooks below. The first two were written in v0.14 and kept off until the hooks spike (Appendix H, P7) had
been run; on 2026-11-15, in a Claude Code 2.1.325 session with the built plugin, the pre-export hook stopped this plugin's build of a
MapSpec with blocking findings, let an unrelated command through untouched, and the reminder hook's note reached the model. The timing hook
came in 1.17.2, so that `map-trace` reports measured times instead of bounds.

| Hook | Event | What it does |
|---|---|---|
| `pre_export_lint.py` | `PreToolUse`, matcher `Bash` | Before this plugin's own `build_map.py`, `webmap.py` or `map_restyle.py --build` runs with a `--spec`, runs the spec lint on that MapSpec. A BLOCK stops the command (exit 2) and the findings go to the model; the stop is also written to the hook log. Any other command passes at once, untouched. |
| `credentials_reminder.py` | `UserPromptSubmit` | Looks for a key, token or password in the person's message with the credentials scanner (`credentials.pasted_finding`, MAP-CRED-007: the `redact` patterns, known key shapes, a Census key in context) and a few bare key shapes. When one is there, adds a reminder to the model: do not repeat, store or use it; keys go in the person's own credential folder (`map-credentials add NAME`), and a key typed into the chat should be revoked and rotated. It never prints the secret and never blocks the message; the hook log gets the time and how many kinds of secret, never the message. |
| `map_timing.py` | `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, matcher `Bash\|mcp__.*device_bash\|mcp__.*geo-mapping.*` | Records the start, end or failure of a call only when it runs one of this plugin's scripts or lints with Python, or is a tool of this plugin's map server: the call's id, the time, the runtime's own duration, the exit code at the head of a failure, and the script or tool name. Never the command, an argument, a result or an error text. Prints nothing and always exits 0. |

**The hook log.** One file per session, `<working folder>/traces/hooks/<session id>.ndjson` (the plugin's `.geo-mapping/` for the folder
the session runs in; `GEO_MAPPING_HOME` overrides), written by `hook_log.py`. It is this plugin's alone: not beside the transcript, and
never another plugin's log, so it cannot collide with the acropolisdocs run-trace. `map-trace` joins it to the transcript by the call's id.

None of the hooks reaches the network. If a hook cannot read what it needs it lets the work go on: the build's own lint is the gate.

**The command form (1.17.2).** Each command is `python3 -c "<a short program>" "${CLAUDE_PLUGIN_ROOT}/hooks/<script>.py"`. The program
drops the working folder from the import path (so a `json.py` in the person's project never runs inside a hook), checks that the script is
there, and runs it. The plain form `python3 "<root>/hooks/x.py"`, which 1.17.0 shipped, exits 2 when the file is missing, and a
`PreToolUse` hook that exits 2 refuses the call: a plugin updated while a chat was open (the chat keeps the old plugin folder, which the
update removed), or a `${CLAUDE_PLUGIN_ROOT}` the host did not expand, refused every Bash call in that chat. Now a missing script exits 0,
silent; only the pre-export check's own verdict exits 2. The edition lint (MAP-ED-005) refuses any other form, and
`tests/test_hooks_1172.py` runs every shipped command through a shell against a missing root, an unexpanded variable and a planted module.

**Windows.** Each command starts with `python3`, which on Windows is usually the Microsoft Store stub (it exits 9009, so the hook would
show an error on every Bash call; never a refusal, since 9009 is not 2). The plugin's server launcher (`mcp/launch.cmd`) runs
`hooks/pin_python.py` with the plugin's own Python on each start, which rewrites `python3` at the head of each command to that interpreter's
full path; hooks are read when a session starts, so the first session after an install or an update can still show those notices once.
Hook commands run in Git Bash on Windows (the documented hook shell there). A Windows machine without Git Bash, where the host falls back to
PowerShell, has not been tested.

## Turn one off

Delete its block from `hooks/hooks.json` in the installed plugin and start a new session; `optional/hooks.json` holds the shipped set to
copy back. Turning off the timing hook means its three blocks (the `map_timing.py` ones); `map-trace` then reports bounds again. To ship a
build without them, empty `src/hooks/hooks.json` before `tools/build_edition.py`; the edition lint (MAP-ED-005) accepts either: every tool
event names a tool in its matcher, the prompt hook, which takes none, runs a script under `${CLAUDE_PLUGIN_ROOT}/hooks/`, and every command
is the guarded form.

## Test them

By hand, from the plugin's folder (no session needed):

```bash
# passes: not this plugin's script
echo '{"tool_name":"Bash","tool_input":{"command":"ls -la"}}' | python3 hooks/pre_export_lint.py; echo "exit $?"
# stops with exit 2: a MapSpec with no message (MAP-SPEC-002); give a path to a real spec of yours to see it pass
printf 'mapspec_version: 1\nid: t1\nlayers: []\n' > /tmp/bad.yaml
echo '{"tool_name":"Bash","cwd":"/tmp","tool_input":{"command":"python3 \"$GMAP_ROOT/scripts/build_map.py\" --spec bad.yaml --out out"}}' | python3 hooks/pre_export_lint.py; echo "exit $?"
# a reminder in JSON; the key itself is not printed
echo '{"prompt":"my key is AIzaFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAK please map"}' | python3 hooks/credentials_reminder.py
# the timing hook: nothing printed, one line in /tmp/gm/traces/hooks/s1.ndjson
echo '{"hook_event_name":"PreToolUse","session_id":"s1","tool_use_id":"t1","tool_name":"Bash","tool_input":{"command":"python3 \"$GMAP_ROOT/scripts/build_map.py\" --spec x.yaml"}}' | GEO_MAPPING_HOME=/tmp/gm python3 hooks/map_timing.py; cat /tmp/gm/traces/hooks/s1.ndjson
```

In a session, with the hooks on: ask for a map from a MapSpec with no `message`, and the build should be stopped before it starts with
the MAP-SPEC-002 line; paste a made-up key such as `GEO_MAPPING_KEY_CARTO_POSITRON=abc123456` in a message, and the answer should not
repeat it and should say where keys go. Then run one unrelated Bash command and one request to another plugin and check that no hook said
anything. Build one map, then run `map-trace`: its `Hooks` line should show the build measured, the stop and the reminder. To check the
update case, keep a chat open, install the plugin again, and run one Bash command in that chat: it must run, not be refused.

`tests/test_self_description.py` and `tests/test_hooks_1172.py` run the same cases in the test suite.
