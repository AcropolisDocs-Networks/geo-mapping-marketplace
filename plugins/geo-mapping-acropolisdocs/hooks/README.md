# Hooks (on since 1.17.0)

`hooks.json` turns on the two hooks below. They were written in v0.14 and kept off until the hooks spike (Appendix H, P7) had been run;
on 2026-11-15, in a Claude Code 2.1.325 session with the built plugin, the pre-export hook stopped this plugin's build of a MapSpec with
blocking findings, let an unrelated command through untouched, and the reminder hook's note reached the model.

| Hook | Event | What it does |
|---|---|---|
| `pre_export_lint.py` | `PreToolUse`, matcher `Bash` | Before this plugin's own `build_map.py`, `webmap.py` or `map_restyle.py --build` runs with a `--spec`, runs the spec lint on that MapSpec. A BLOCK stops the command (exit 2) and the findings go to the model. Any other command passes at once, untouched. |
| `credentials_reminder.py` | `UserPromptSubmit` | Looks for a key, token or password in the person's message with the credentials scanner (`credentials.pasted_finding`, MAP-CRED-007: the `redact` patterns, known key shapes, a Census key in context) and a few bare key shapes. When one is there, adds a reminder to the model: do not repeat, store or use it; keys go in the person's own credential folder (`map-credentials add NAME`), and a key typed into the chat should be revoked and rotated. It never prints the secret and never blocks the message. |

Neither writes a file or reaches the network. If a hook cannot read what it needs it lets the work go on: the build's own lint is the gate.

**Windows.** Each command starts with `python3`, which on Windows is usually the Microsoft Store stub (it exits 9009, so the hook would
show an error on every Bash call). The plugin's server launcher (`mcp/launch.cmd`) runs `hooks/pin_python.py` with the plugin's own Python
on each start, which rewrites the commands to that interpreter's full path; hooks are read when a session starts, so the first session
after an install or an update can still show those notices once.

## Turn one off

Delete its block from `hooks/hooks.json` in the installed plugin and start a new session; `optional/hooks.json` holds the shipped pair to
copy back. To ship a build without them, empty `src/hooks/hooks.json` before `tools/build_edition.py`; the edition lint (MAP-ED-005)
accepts either: the Bash hook has a matcher, and the prompt hook, which takes none, runs a script under `${CLAUDE_PLUGIN_ROOT}`.

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
```

In a session, with the hooks on: ask for a map from a MapSpec with no `message`, and the build should be stopped before it starts with
the MAP-SPEC-002 line; paste a made-up key such as `GEO_MAPPING_KEY_CARTO_POSITRON=abc123456` in a message, and the answer should not
repeat it and should say where keys go. Then run one unrelated Bash command and one request to another plugin and check that neither
hook said anything: that is the P7 result to record in Appendix H.6.

`tests/test_self_description.py` runs the same cases in the test suite.
