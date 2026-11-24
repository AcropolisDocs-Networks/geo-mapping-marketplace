"""The short entries a person types by name (1.14.1).

Until 1.14.0 these were flat files in the plugin's commands/ folder, which Claude Code now reports as a legacy layout on install. From
1.14.1 each is a skill of its own, skills/<name>/SKILL.md, with the same frontmatter (disable-model-invocation, argument-hint) and the same
slash command, /geo-mapping-<brand>:<name>. They stay a group apart from the map skills: most hand the request straight to a skill
(make -> map-create), so the edition lint, the guide and the session trace still count them as the plugin's commands.

One list, read by scripts/map_trace.py here and by the build tools (tools/edition_lint.py, tools/overview_facts.py).
"""
from __future__ import annotations

ENTRY_SKILLS: frozenset[str] = frozenset({
    "brand", "export", "find", "make", "map-run", "map-token-use", "service", "style", "web",
})
