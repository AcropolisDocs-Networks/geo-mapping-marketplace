"""Brand lint, MAP-BRAND-* (spec 12.5). Pure checks on text and packs; no network, no writes.

Each finding is {"id", "severity", "message", "detail"}. Severity is BLOCK, WARN or INFO.
"""
from __future__ import annotations

import html as htmllib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import brandlib as bl  # noqa: E402
import gmap_common as gc  # noqa: E402

_HEX = re.compile(r"(?<![\w&])#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![\w-])")
_RGB = re.compile(r"rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})")
_FAMILY = re.compile(r"font-family\s*[:=]\s*[\"']?([^;}\"<>]+)", re.I)
_NEUTRAL_WORDS = {"none", "transparent", "currentcolor", "inherit", "initial", "unset", "context-fill", "context-stroke"}
_NAMED_COLOR_ATTR = re.compile(r"\b(?:fill|stroke|stop-color|flood-color|color)\s*[=:]\s*[\"']?\s*([a-zA-Z]+)\b")


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def _norm(h: str) -> str:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h[:6].upper()


def allowed_colors(slug: str) -> set[str]:
    pack = bl.load_pack(slug)
    out: set[str] = set()
    for mode in pack["tokens"]["modes"].values():
        for v in mode.values():
            stack = [v]
            while stack:
                x = stack.pop()
                if isinstance(x, str) and x.startswith("#"):
                    out.add(_norm(x))
                elif isinstance(x, list):
                    stack.extend(x)
                elif isinstance(x, dict):
                    stack.extend(x.values())
    out |= {_norm(c) for c in bl.logo_colors(pack["brand"])}
    out |= derived_colors(slug)
    if slug not in _DIMMED:
        import map_look                                      # 0.22.0: the dimmed twin of every colour, for the rest of a highlighted layer
        _DIMMED[slug] = {_norm(c) for c in map_look.dim_colors(sorted(out))}
    return out | _DIMMED[slug]


_DIMMED: dict[str, set[str]] = {}


_DERIVED: dict[str, set[str]] = {}


def derived_colors(slug: str) -> set[str]:
    """The one derived palette (M15): the bivariate grids computed from each theme's tokens (bivariate.grid_for, colorlib.bivariate_grid).
    Recomputed here from the same tokens, never read from the map, so a colour the renderer made up still fails."""
    if slug not in _DERIVED:
        import bivariate
        pack = bl.load_pack(slug)
        import map_look                                      # 0.17.0: the generated ramps of the auto look, recomputed from the same tokens
        _DERIVED[slug] = ({_norm(c) for mode in pack["tokens"]["modes"].values() for c in bivariate.derived_colors(mode)}
                          | {_norm(c) for mode in pack["tokens"]["modes"].values() for c in map_look.derived_colors(mode)})
    return set(_DERIVED[slug])


def colors_in(text: str) -> set[str]:
    found: set[str] = set()
    for m in _HEX.finditer(text):
        start = m.start()
        before = text[max(0, start - 8):start].lower()
        if before.endswith("url(") or before.endswith("href=\"") or before.endswith("href='"):
            continue
        found.add(_norm(m.group(0)))
    for m in _RGB.finditer(text):
        r, g, b = (min(255, int(x)) for x in m.groups())
        found.add(f"#{r:02X}{g:02X}{b:02X}")
    return found


def named_colors_in(text: str) -> set[str]:
    try:
        from matplotlib.colors import CSS4_COLORS
        names = set(CSS4_COLORS)
    except Exception:  # noqa: BLE001
        names = {"red", "green", "blue", "black", "white", "gray", "grey", "orange", "yellow", "purple", "pink", "brown"}
    return {m.group(1).lower() for m in _NAMED_COLOR_ATTR.finditer(text)
            if m.group(1).lower() in names and m.group(1).lower() not in _NEUTRAL_WORDS}


_STYLE_ALLOWED: dict = {}


def style_allowed_colors(slug: str, style) -> set[str]:
    """1.4.0 (R2-PACK): what MAP-BRAND-001 allows under a style pack that replaces or derives the palette: the pack's resolved tokens in every
    mode, the colours the looks and the bivariate grids derive from them, their dimmed twins, and the brand's logo colours (identity is kept)."""
    if style is None or getattr(style, "palette", "brand") == "brand":
        return allowed_colors(slug)
    key = (slug, style.id, style.sha256)
    if key not in _STYLE_ALLOWED:
        import bivariate
        import map_look
        import map_style
        pack = bl.load_pack(slug)
        modes = map_style.palette_modes(style, pack["tokens"]["modes"])
        out = {_norm(c) for m in modes.values() for c in map_style._walk_hex(m)}
        out |= {_norm(c) for c in bl.logo_colors(pack["brand"])}
        out |= {_norm(c) for m in modes.values() for c in bivariate.derived_colors(m)}
        out |= {_norm(c) for m in modes.values() for c in map_look.derived_colors(m)}
        cap = (style.data.get("derive") or {}).get("chroma_cap") if style.palette == "brand-derived" else None
        if cap is not None:                                     # the look's generated ramps, held to the pack's chroma cap (map_style.after_look)
            out |= {_norm(map_style.cap_color(c, cap)) for c in list(out)}
        out |= {_norm(c) for c in map_look.dim_colors(sorted(out))}
        _STYLE_ALLOWED[key] = out
    return _STYLE_ALLOWED[key]


def check_palette(text: str, slug: str, style=None) -> list[dict]:
    """MAP-BRAND-001: declared and derived tokens only (1.4.0: the resolved style pack's, which under `default` are the brand's)."""
    bad = sorted(colors_in(text) - style_allowed_colors(slug, style))
    named = sorted(named_colors_in(text))
    out = []
    if bad:
        where = f"the {slug} tokens" if style is None or getattr(style, "palette", "brand") == "brand" else f"the {style.id} style pack's tokens"
        out.append(finding("MAP-BRAND-001", "BLOCK", f"{len(bad)} color(s) outside {where}", bad[:20]))
    if named:
        out.append(finding("MAP-BRAND-001", "BLOCK", "named CSS colors are not allowed; use a token", named))
    return out


def check_fonts(text: str, families: set[str] | None = None) -> list[dict]:
    """MAP-BRAND-003: the resolved style pack's families only, each embedded (1.4.0). Under `default` that is Inter only, as before."""
    allowed = {f.lower() for f in (families or {"Inter"})}
    generic = {"system-ui", "sans-serif", "serif", "monospace", "cursive"}
    out = []
    used: set[str] = set()
    for m in _FAMILY.finditer(text):
        for fam in m.group(1).split(","):
            fam = fam.strip().strip("'\"").strip()
            if fam:
                used.add(fam)
    other = sorted(f for f in used if f.lower() not in allowed | generic)
    label = "Inter" if allowed == {"inter"} else "the style pack's (" + ", ".join(sorted(families or [])) + ")"
    if other:
        out.append(finding("MAP-BRAND-003", "BLOCK", f"font family other than {label}", other))
    embedded = {m.group(1).strip().strip("'\"").lower() for m in re.finditer(r"@font-face\s*\{[^}]*?font-family\s*:\s*([^;}]+)", text, re.I | re.S)}
    for fam in sorted({f for f in used if f.lower() in allowed}):
        if fam.lower() not in embedded:
            if fam.lower() == "inter":
                out.append(finding("MAP-BRAND-003", "WARN", "Inter is referenced but not embedded as @font-face", None))
            else:
                out.append(finding("MAP-BRAND-003", "BLOCK", f"the style pack's font {fam} is used but not embedded as @font-face", None))
    if embedded - allowed:
        out.append(finding("MAP-BRAND-003", "BLOCK", f"@font-face for a family other than {label}", sorted(embedded - allowed)))
    return out


def visible_text(text: str) -> str:
    t = re.sub(r"(?is)<(metadata|script|style|title|desc)\b.*?</\1>", " ", text)
    t = re.sub(r"(?is)<meta\b[^>]*>", " ", t)
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", t))


def check_hidden_accreditation(text: str, slug: str, manifest: dict) -> list[dict]:
    """MAP-BRAND-004 (present in metadata) and MAP-BRAND-011 (never visible)."""
    out = []
    has_attr = re.search(r"data-accreditation\s*=", text) is not None
    has_meta = re.search(r"<meta[^>]+name=[\"']accreditation[\"']", text, re.I) is not None
    has_rdf = re.search(r"accreditation", text, re.I) is not None
    if not (has_attr or has_meta or has_rdf):
        out.append(finding("MAP-BRAND-004", "BLOCK", "hidden accreditation properties missing", None))
    house = manifest["house_brand"]
    if slug != house:
        pack = bl.load_pack(house) if (gc.brand_dir(house) / "PACK.json").is_file() else None
        acc = (pack["pack"]["accreditation"] if pack else
               bl.load_pack(slug)["pack"]["accreditation"])
        vis = visible_text(text)
        needles = [n for n in {acc.get("text"), acc.get("full"), acc.get("url")} if n]
        hits = [n for n in needles if n.lower() in vis.lower()]
        alt = re.findall(r"(?:alt|aria-label|title)\s*=\s*[\"']([^\"']*)[\"']", text, re.I)
        hits += [n for n in needles if any(n.lower() in a.lower() for a in alt)]
        if hits:
            out.append(finding("MAP-BRAND-011", "BLOCK", "visible accreditation: the house credit appears in rendered text or alt text", sorted(set(hits))))
    return out


def check_stamps(text: str, slug: str, manifest: dict) -> list[dict]:
    """MAP-BRAND-010: brand stamps on SVG/HTML roots."""
    out = []
    if not re.search(r"data-map-brand\s*=\s*[\"']" + re.escape(slug) + r"[\"']", text):
        out.append(finding("MAP-BRAND-010", "BLOCK", f"data-map-brand=\"{slug}\" stamp missing", None))
    if not re.search(r"data-map-edition\s*=\s*[\"']" + re.escape(manifest["plugin_name"]) + r"[\"']", text):
        out.append(finding("MAP-BRAND-010", "BLOCK", f"data-map-edition=\"{manifest['plugin_name']}\" stamp missing", None))
    return out


def check_voice(text: str, slug: str) -> list[dict]:
    """MAP-BRAND-005: no prohibited terms. A qualifier such as '(when self-referring)' makes the hit a WARN."""
    out = []
    prohibited = bl.load_pack(slug)["brand"].get("voice", {}).get("prohibited", [])
    for entry in prohibited:
        term = re.sub(r"\s*\(.*?\)\s*$", "", entry).strip()
        qualified = entry != term
        if not term:
            continue
        for m in re.finditer(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.I):
            line_start = text.rfind("\n", 0, m.start()) + 1
            snippet = text[line_start:text.find("\n", m.end()) if "\n" in text[m.end():] else len(text)].strip()[:120]
            out.append(finding("MAP-BRAND-005", "WARN" if qualified else "BLOCK",
                               f"prohibited term '{term}'" + (" (rule applies when self-referring; check context)" if qualified else ""),
                               snippet))
            break
    return out


def check_bundled(slug: str, manifest: dict) -> list[dict]:
    """MAP-BRAND-013: the resolved brand is one this plugin can draw: its shipped brand or a pack the person added (1.7.0)."""
    if slug not in bl.available(manifest):
        return [finding("MAP-BRAND-013", "BLOCK", f"brand '{slug}' is not available in {manifest['plugin_name']}", bl.available(manifest))]
    return []


def find_pack_source(pack: dict, roots: list | None = None) -> Path | None:
    """0.15.0: the brand file a pack was imported from, if the documents plugin is installed here: the recorded root first, then the
    plugin caches under ~/.claude/plugins (the folder name is the plugin's), at the same path inside the plugin."""
    import glob
    import os
    src = pack.get("source") or {}
    rel, plugin = src.get("path"), src.get("plugin")
    if not rel or not plugin or ".." in Path(rel).parts:
        return None
    cands = []
    if src.get("root"):
        cands.append(Path(src["root"]) / rel)
    for r in roots if roots is not None else [Path(os.path.expanduser("~")) / ".claude" / "plugins"]:
        cands += [Path(p) for p in sorted(glob.glob(str(Path(r) / "**" / plugin / rel), recursive=True))[:20]]
    return next((c for c in cands if _readable_file(c)), None)


def _readable_file(p: Path) -> bool:
    """0.16.1: a candidate this process may not read (a sandbox that hides another plugin's cache) is not the source; is_file() raises
    PermissionError on Python 3.10 instead of answering False, which crashed map-doctor."""
    try:
        return p.is_file() and os.access(p, os.R_OK)
    except OSError:
        return False


def check_pack_drift(slug: str, roots: list | None = None) -> list[dict]:
    """MAP-BRAND-016 (0.15.0): the brand in the documents plugin has changed since this pack was imported (its file's SHA-256 is not the one
    PACK.json recorded). Run-time drift report; re-import with tools/import_brand.py. Nothing is reported when the source is not installed here."""
    d = gc.brand_dir(slug)
    try:
        pack = json.loads((d / "PACK.json").read_text())
    except (OSError, ValueError):
        return []
    if pack.get("origin") == "user":                    # 1.7.0: a pack the person added has no source plugin to drift from
        return []
    path = find_pack_source(pack, roots)
    if path is None:
        return [finding("MAP-BRAND-016", "INFO", f"{slug}: the brand's source ({(pack.get('source') or {}).get('plugin')} plugin) is not installed here, so drift is not checked")]
    try:
        got = bl.sha256_file(path)
    except OSError:
        return [finding("MAP-BRAND-016", "INFO", f"{slug}: the brand's source could not be read here, so drift is not checked")]
    if got == pack.get("source_sha256"):
        return []
    ver = None
    for up in path.parents:
        pj = up / ".claude-plugin" / "plugin.json"
        if pj.is_file():
            try:
                ver = json.loads(pj.read_text()).get("version")
            except (OSError, ValueError):
                pass
            break
    return [finding("MAP-BRAND-016", "WARN", f"{slug}: the brand in the {(pack.get('source') or {}).get('plugin')} plugin"
                    + (f" ({ver})" if ver else "") + f" has changed since this pack was imported on {str(pack.get('imported_on', '?'))[:10]} "
                    f"(from version {(pack.get('source') or {}).get('version')}); maps keep the imported pack until it is re-imported with tools/import_brand.py",
                    {"recorded": str(pack.get("source_sha256"))[:12], "now": got[:12]})]


def check_pack_integrity(slug: str) -> list[dict]:
    """MAP-BRAND-007: copied files match PACK.json hashes; brand.json validates against the pinned schema."""
    out = []
    d = gc.brand_dir(slug)
    pack = json.loads((d / "PACK.json").read_text())
    for name, want in pack["files"].items():
        got = bl.sha256_file(d / name) if (d / name).is_file() else None
        if got != want:
            out.append(finding("MAP-BRAND-007", "BLOCK", f"{slug}/{name} does not match PACK.json", {"want": want[:12], "got": (got or 'missing')[:12]}))
    try:
        import jsonschema
        schema = json.loads((gc.plugin_root() / "schemas" / "brand.schema.json").read_text())
        errs = list(jsonschema.validators.validator_for(schema)(schema).iter_errors(json.loads((d / "brand.json").read_text())))
        if errs:
            out.append(finding("MAP-BRAND-007", "BLOCK", f"{slug}/brand.json fails the pinned brand schema", errs[0].message))
    except ImportError:
        out.append(finding("MAP-BRAND-007", "WARN", "jsonschema is missing; brand.json was not schema-checked", None))
    return out


def worst(findings: list[dict]) -> int:
    return gc.EXIT_LINT if any(f["severity"] == "BLOCK" for f in findings) else gc.EXIT_OK
