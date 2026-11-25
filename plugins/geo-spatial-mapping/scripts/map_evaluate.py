#!/usr/bin/env python3
"""map_evaluate: grade finished maps, observe only (2.2.0; the map-evaluate module).

It looks at the rendered output only (a still, an SVG or PDF sheet, a web map's default view, a video's frames, a story's panels) and
grades it against the rubric in evaluation/rubric.yaml, so the plugin's functions can be improved later. It never edits a map, never
feeds a build, and nothing that makes a map reads what it writes (owner decision D1; the release gate's test_evaluator_isolation).

    map_evaluate.py status   --run DIR                 what is still needed: the reads each fresh agent must answer, with prompt and answer paths
    map_evaluate.py save     --run DIR --read ID --file F   store an agent's JSON answer unchanged (when the agent could not write the file itself)
    map_evaluate.py record   --run DIR [--trace F.json]     score the reads, write the scorecards, the history line and the report page;
                                                        with --trace, add the Map quality section to that map-trace page
    map_evaluate.py measure  FILE [--svg F]             the measurements alone (M-01 to M-12), as JSON
    map_evaluate.py trend    [--out F.html]             Effectiveness and Craft over time, from the history, within one rubric major version
    map_evaluate.py anchors  --set anchors.yaml --owner owner-scores.yaml [--run DIR ...]   agreement with the owner's scores
    map_evaluate.py review   [--days N] [--owner F]     the 10-day rubric review: unstable criteria, n/a proposals, flat criteria, disagreements

The packets themselves are made by the `map-evaluate` pipeline (`pipelines.py plan|run map-evaluate`), which confirms the cost first.

Where things go: <working folder>/evaluations/<run id>/ holds the packets, the reads, the scorecards (<output>.eval.json), the report and
the improvement backlog; <working folder>/evaluations/history.jsonl holds one line per evaluated output. Nothing goes beside a deliverable,
into a provenance sidecar or into a package (spec I4), and no image goes into the history (R13).

Exit codes: 0 done; 2 usage; 3 a read is missing or the run cannot be scored; 1 a runtime failure.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html as _html
import io
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

SCHEMA = "map-eval/1"
MEASURE_VERSION = "1"
EVAL_DIRNAME = "evaluations"
READ_LONG_SIDE = 2000          # px: the picture an agent reads (big enough for the smallest print text, small enough to keep reads cheap)
MEASURE_LONG_SIDE = 1600       # px: the copy the measurements run on
MASK_DPI = 72.0                # the plugin SVG's layer renders (M-02, M-09, M-10)
TEXT_DPI = 150.0               # the text renders (M-05 locations, M-06)
THUMB_WIDTH = 400              # px: feed or back-of-room size (M-11)
VIDEO_STEP_S = 5.0
MAX_VIDEO_FRAMES = 24
MAX_PDF_PAGES = 6
WEB_SIZES = (("desktop", 1440, 900), ("phone", 390, 844))
STILL_EXT = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".gif"}
OUTPUT_EXT = STILL_EXT | VIDEO_EXT | {".svg", ".pdf", ".html", ".htm"}
SKIP_NAME = re.compile(r"(_mute|_draft|_preview|\.provenance|\.terms|_thumb)(\.|_|$)|^preview\.|^mute\.", re.I)
SKIP_PARTS = {".geo-mapping", "runs", "qa-build", "frames", "__MACOSX", EVAL_DIRNAME, "node_modules"}
SEQ_STEM = re.compile(r"^(.*?)[_-](\d{1,3})$")
FURNITURE = ("gm-title", "gm-legend", "gm-inset", "gm-logo", "gm-north", "gm-footer", "gm-scalebar", "gm-callout", "gm-data-table",
             "gm-provider-logo", "gm-chart", "gm-story")
CONTEXT = ("gm-base", "gm-names", "gm-graticule", "gm-focus-mask", "gm-focus-lift", "gm-relief")
DATA_TEXT = ("gm-labels",)
AREA_ROLES = {"choropleth", "hexbin", "grid"}
POINT_ROLES = {"points", "proportional"}
TERRAIN_ROLES = {"hillshade", "contour"}
SURFACE_ROLES = {"raster", "heat"}
FINDING_WORDS = re.compile(r"\b(highest|lowest|most|least|more|fewer|less|than|twice|doubled|half|majority|minority|concentrat\w*|cluster\w*|gap\w*|"
                           r"increase\w*|decrease\w*|grew|fell|rose|drop\w*|lag\w*|lead\w*|only|every|none|\d+\s?%|\d+ percent)\b", re.I)
IMPERATIVE = re.compile(r"^\s*(please\s+)?(move|make|change|add|remove|use|increase|decrease|reduce|shrink|enlarge|put|drop|try|consider|replace|"
                        r"recolou?r|darken|lighten|shift|swap|delete|rewrite|rename|fix|adjust|set|turn|place|bump|raise|lower|thicken|thin|crop|"
                        r"zoom|tighten|loosen|enlarge|simplify|declutter|label|relabel|recenter|recentre)\b", re.I)
SECOND_PERSON = re.compile(r"\b(you|your|yours)\b", re.I)
THIS_MAP = re.compile(r"\b(this|the) (map|sheet|picture|output|frame) (should|needs|must|could)\b", re.I)


class EvalError(Exception):
    def __init__(self, msg: str, fid: str = "MAP-EVAL-005"):
        super().__init__(msg)
        self.fid = fid


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def esc(x) -> str:
    return _html.escape("" if x is None else str(x), quote=True)


def plugin_version() -> str | None:
    try:
        return gc.build_manifest().get("version")
    except Exception:  # noqa: BLE001
        return None


def evaluations_root(create: bool = False) -> Path:
    p = gc.workdir() / EVAL_DIRNAME
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------------------------------------------- the rubric
def rubric_path() -> Path:
    for p in (HERE.parent / "evaluation" / "rubric.yaml", HERE.parent.parent / "src" / "evaluation" / "rubric.yaml"):
        if p.is_file():
            return p
    raise EvalError("evaluation/rubric.yaml is missing from the plugin", "MAP-EVAL-010")


def load_rubric(path: Path | None = None) -> dict:
    """The rubric, checked: every area weighted for every outcome, every criterion with its 5/3/1 descriptions, every component known."""
    rub = gc.load_yaml(path or rubric_path())
    problems = []
    outcomes = list(rub.get("outcomes") or {})
    comps = set(rub.get("components") or {})
    for a, w in (rub.get("weights") or {}).items():
        missing = [o for o in outcomes if o not in (w or {})]
        if missing:
            problems.append(f"weights.{a} lacks {missing}")
    for a, area in (rub.get("areas") or {}).items():
        if a not in (rub.get("weights") or {}):
            problems.append(f"area {a} has no weights")
        for cid, c in (area.get("criteria") or {}).items():
            if not cid.startswith(a + "-"):
                problems.append(f"criterion {cid} is not named for its area {a}")
            for k in (5, 3, 1, "name"):
                if k not in c:
                    problems.append(f"{cid} lacks {k}")
            for comp in c.get("components") or []:
                if comp not in comps:
                    problems.append(f"{cid} names an unknown component {comp}")
    for h in (rub.get("headlines") or {}).values():
        for a in h:
            if a not in (rub.get("areas") or {}):
                problems.append(f"headline area {a} is not an area")
    if problems:
        raise EvalError("the rubric is not valid: " + "; ".join(problems), "MAP-EVAL-010")
    return rub


def criteria(rub: dict) -> dict:
    out = {}
    for a, area in rub["areas"].items():
        for cid, c in area["criteria"].items():
            out[cid] = {**c, "id": cid, "area": a}
    return out


def rubric_major(v: str) -> str:
    return str(v).split(".")[0]


# ---------------------------------------------------------------------------------------------------------------- finding the outputs
def _skip(p: Path) -> bool:
    return bool(SKIP_NAME.search(p.name)) or bool(SKIP_PARTS & set(p.parts[:-1])) or p.suffix.lower() not in OUTPUT_EXT


def discover(paths: list[str | Path], specs: dict | None = None) -> tuple[list[dict], list[dict]]:
    """Group finished files into outputs: one map's .svg/.png/.pdf are one output (the PNG is what is looked at, the SVG gives structure);
    numbered pictures sharing a stem (a carousel, story panels) are one sequence. -> (outputs, findings)."""
    fs: list[dict] = []
    files = []
    for x in paths:
        p = Path(x)
        if p.is_dir():
            files += [q for q in sorted(p.rglob("*")) if q.is_file() and not _skip(q)]
        elif p.is_file():
            if _skip(p):
                fs.append(finding("MAP-EVAL-009", "INFO", f"{p.name}: a working file (preview, mute, draft, sidecar), not an output; left out"))
            else:
                files.append(p)
        else:
            fs.append(finding("MAP-EVAL-009", "WARN", f"{p.name} was not found; left out"))
    by_stem: dict[tuple[str, str], list[Path]] = {}
    for p in files:
        by_stem.setdefault((str(p.parent), p.stem), []).append(p)
    outputs, seq = [], {}
    for (parent, stem), group in by_stem.items():
        exts = {q.suffix.lower(): q for q in group}
        m = SEQ_STEM.match(stem)
        if m and set(exts) & STILL_EXT and not (set(exts) - STILL_EXT - {".svg"}):
            seq.setdefault((parent, m.group(1)), []).append((int(m.group(2)), next(q for e, q in exts.items() if e in STILL_EXT)))
            continue
        svg = exts.get(".svg")
        if ".html" in exts or ".htm" in exts:
            outputs.append({"kind": "web", "main": exts.get(".html") or exts.get(".htm"), "svg": None})
            group = [q for q in group if q.suffix.lower() not in (".html", ".htm")]
            exts = {q.suffix.lower(): q for q in group}
        vids = [q for q in group if q.suffix.lower() in VIDEO_EXT]
        for v in vids:
            outputs.append({"kind": "video", "main": v, "svg": None})
        still = next((exts[e] for e in (".png", ".jpg", ".jpeg", ".webp") if e in exts), None)
        if still:
            outputs.append({"kind": "still", "main": still, "svg": svg})
        elif ".pdf" in exts:
            outputs.append({"kind": "pdf", "main": exts[".pdf"], "svg": svg})
        elif svg:
            outputs.append({"kind": "svg", "main": svg, "svg": svg})
    for (parent, base), items in seq.items():
        items.sort()
        if len(items) == 1:
            outputs.append({"kind": "still", "main": items[0][1], "svg": None})
        else:
            outputs.append({"kind": "sequence", "main": items[0][1], "members": [q for _, q in items], "svg": None, "stem": base})
    seen: dict[str, int] = {}
    for o in outputs:
        stem = o.get("stem") or Path(o["main"]).stem
        oid = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:60] or "output"
        if o["kind"] in ("web", "video"):
            oid += "-" + o["kind"]
        n = seen.get(oid, 0)
        seen[oid] = n + 1
        o["id"] = oid if not n else f"{oid}-{n + 1}"
        o["spec"] = (specs or {}).get(str(o["main"])) or _spec_beside(Path(o["main"]))
        o["files"] = [str(q) for q in o.get("members") or [o["main"]]]
    outputs.sort(key=lambda o: o["id"])
    return outputs, fs


def sidecar_spec(p: Path) -> dict | None:
    """2.4.0 (map-trace MT-05): the `spec` block of the provenance sidecar beside a finished file ({path, sha256, id, message}), or None."""
    p = Path(p)
    for side in (p.with_name(p.name + ".provenance.json"), p.with_name(p.stem + ".provenance.json")):   # 3.4.0 (F-6): x.mp4.provenance.json too
        try:
            s = json.loads(side.read_text(encoding="utf-8")).get("spec")
        except (OSError, ValueError, AttributeError):
            continue
        return s if isinstance(s, dict) else None
    return None


def _spec_beside(p: Path) -> str | None:
    """The MapSpec a finished file came from: the one its provenance sidecar names (2.4.0, MT-05), else the one a deliverable folder carries
    beside its map (map-build packages it as mapspec.yaml)."""
    s = sidecar_spec(p)
    if s and s.get("path"):
        q = (Path(p).parent / s["path"])
        if q.is_file():
            return str(q.resolve())
    if s:                                   # 3.4.0 (F-6): the files were moved or copied since the build; find the spec by its hash
        cands = [Path(s["abs_path"])] if s.get("abs_path") else []
        name = s.get("name") or Path(str(s.get("path") or "")).name
        if name:
            cands += [up / name for up in [Path(p).parent, *list(Path(p).parents)[:3]]]
        for q in cands:
            try:
                if q.is_file() and (not s.get("sha256") or _sha(q) == s["sha256"]):
                    return str(q.resolve())
            except OSError:
                continue
    for name in ("mapspec.yaml", "MapSpec.yaml", f"{p.stem}.mapspec.yaml"):
        q = p.parent / name
        if q.is_file():
            return str(q)
    return None


def frame_count_estimate(o: dict) -> int:
    k = o["kind"]
    if k == "sequence":
        return len(o["members"])
    if k == "web":
        return 2 * len(WEB_SIZES)
    if k == "pdf":
        try:
            import pdf_stack
            n = len(pdf_stack.read_pdf(Path(o["main"])).pages)
        except Exception:  # noqa: BLE001
            n = 1
        return min(n, MAX_PDF_PAGES)
    if k == "video":
        d = video_duration(Path(o["main"])) or 30.0
        beats = len(cue_times(Path(o["main"])))
        return min(MAX_VIDEO_FRAMES, 2 + beats + int(d // VIDEO_STEP_S))
    return 1


# ---------------------------------------------------------------------------------------------------------------- the intent card
def svg_facts(svg_text: str) -> dict:
    """What the plugin's own SVG says about itself: message (dc:description), template, style pack, scale, legend, logo, frame, text sizes."""
    out: dict = {}
    m = re.search(r"<dc:description>(.*?)</dc:description>", svg_text, re.S)
    if m and not m.group(1).startswith("http"):
        out["message"] = _html.unescape(m.group(1)).strip()
    m = re.search(r"<geo:template>(.*?)</geo:template>", svg_text)
    if m:
        out["template"] = m.group(1).strip()
    m = re.search(r'data-map-style-pack="([^"]+)"', svg_text)
    out["style_pack"] = m.group(1) if m else None
    m = re.search(r"Scale about 1:([\d,]+)", svg_text)
    out["scale_denominator"] = int(m.group(1).replace(",", "")) if m else None
    out["has_legend"] = 'class="gm-legend' in svg_text
    out["branded"] = 'class="gm-logo' in svg_text or 'data-map-brand="' in svg_text
    out["focus_fill"] = 'data-focus="mask"' in svg_text          # the focus mask is drawn only under a classed area fill (map_focus.FOCUS_ROLES)
    out["layers"] = re.findall(r'<g id="gm-layer-([^"]+)" data-layer="[^"]*"(?: data-role="([a-z]+)")?', svg_text)
    m = re.search(r'<svg\b[^>]*viewBox="([\d.\s-]+)"[^>]*width="([\d.]+)mm"', svg_text)
    if m:
        vb = [float(v) for v in m.group(1).split()]
        out["sheet_mm"] = [float(m.group(2)), float(m.group(2)) * vb[3] / vb[2]]
        out["unit_mm"] = float(m.group(2)) / vb[2]
    m = re.search(r'<clipPath id="gm-clip"><rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"', svg_text)
    if m and out.get("sheet_mm"):
        x, y, w, h = (float(v) * out.get("unit_mm", 1.0) for v in m.groups())
        W, H = out["sheet_mm"]
        out["map_frame_pct"] = [round(100 * x / W, 2), round(100 * y / H, 2), round(100 * (x + w) / W, 2), round(100 * (y + h) / H, 2)]
    return out


def medium_of(kind: str, template: str | None, path: Path) -> str:
    t = (template or "").lower()
    name = path.name.lower()
    if kind == "web":
        return "web"
    if kind == "video":
        return "video"
    if kind == "sequence" or "carousel" in name or "panel" in name:
        return "story"
    if "slide" in t or "16x9" in t:
        return "slide"
    if "social" in t:
        return "social"
    if t == "story" or "story" in name:
        return "story"
    if "slide" in name or "16x9" in name:
        return "slide"
    if "social" in name:
        return "social"
    return "print"


def family_of(roles: list[str], kind: str, spec: dict | None) -> str:
    r = set(roles)
    if spec and any((l.get("source") or {}).get("type") in ("gtfs", "gtfs-rt") for l in spec.get("layers") or []):
        return "transit"
    if spec and any("isochrone" in json.dumps(l.get("ops") or []) or l.get("role") == "isochrone" for l in spec.get("layers") or []):
        return "isochrone"
    if r & AREA_ROLES:
        return "choropleth"
    if r & POINT_ROLES:
        return "points"
    if "lines" in r:
        return "network"
    if r & SURFACE_ROLES:
        return "surface"
    if r & TERRAIN_ROLES:
        return "terrain"
    if "polygons" in r:
        return "areas"
    return {"web": "web", "video": "video", "sequence": "story"}.get(kind, "unknown")


def infer_outcome(spec: dict | None, roles: list[str], medium: str, message: str, family: str) -> tuple[str | None, str | None, str]:
    """(primary, secondary, source). Read from the MapSpec's own fields and the picture's kind; never from a field the evaluator added
    (the MapSpec has none, spec I2). None when nothing says: the cold read's guess is used then (MAP-EVAL-007)."""
    # 4.2.0: the MapSpec says what the map is for (`outcome`, `secondary_outcome`): nothing is guessed when it does. The field is written
    # when the map is asked for, by the person's request; it is not a field the evaluator added, and the evaluator never writes it.
    stated = str((spec or {}).get("outcome") or "").strip().lower()
    if stated in ("explain", "compare", "locate", "inventory", "monitor", "explore", "sequence", "evoke"):
        second = str((spec or {}).get("secondary_outcome") or "").strip().lower()
        return stated, (second if second in ("explain", "compare", "locate", "inventory", "monitor", "explore", "sequence", "evoke") and second != stated else None), "mapspec"
    task = " ".join(str(t).lower() for t in (spec or {}).get("task") or [])
    named = [o for o in ("explain", "compare", "locate", "inventory", "monitor", "explore", "sequence", "evoke") if o in task]
    if named:
        return named[0], (named[1] if len(named) > 1 else None), "task"
    finding_led = bool(message and FINDING_WORDS.search(message))
    audience = str((spec or {}).get("audience") or "").lower()
    status = spec and any(re.search(r"status|rag|state", str((l.get("legend") or {}).get("title") or l.get("value_field") or ""), re.I)
                          for l in spec.get("layers") or [])
    if medium in ("video", "story") or (spec or {}).get("story"):
        return "sequence", ("explain" if finding_led else None), "inferred"
    if medium == "web":
        return "explore", ("explain" if finding_led else ("compare" if family == "choropleth" else None)), "inferred"
    if status:
        return "monitor", None, "inferred"
    if family == "choropleth" or family == "surface":
        return ("explain", "compare", "inferred") if finding_led else ("compare", None, "inferred")
    if family in ("points", "network", "transit", "areas", "isochrone"):
        if finding_led:
            return "explain", ("locate" if family == "points" else None), "inferred"
        return ("inventory" if audience == "internal" else "locate"), None, "inferred"
    if family == "terrain":
        return ("explain", None, "inferred") if finding_led else ("evoke", None, "inferred")
    if message:
        return ("explain", None, "inferred") if finding_led else (None, None, "unknown")
    return None, None, "unknown"


def intent_card(o: dict, opts: dict | None = None) -> dict:
    opts = opts or {}
    spec = None
    if o.get("spec"):
        try:
            spec = gc.load_mapspec(Path(o["spec"]))
        except Exception:  # noqa: BLE001 - an unreadable MapSpec: the picture's own metadata stands in
            spec = None
    facts = {}
    if o.get("svg"):
        try:
            facts = svg_facts(Path(o["svg"]).read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            facts = {}
    elif Path(o["main"]).suffix.lower() == ".svg":
        facts = svg_facts(Path(o["main"]).read_text(encoding="utf-8", errors="replace"))
    roles = [str(l.get("role") or "") for l in (spec or {}).get("layers") or [] if l.get("draw", True) is not False]
    if not roles and facts.get("layers"):
        roles = [("context" if r == "context" else "contour" if r == "contour" else "data") for _, r in facts["layers"]]
    side = sidecar_spec(Path(o["main"])) if not spec else None          # 2.4.0 (MT-05): the message as the build recorded it
    message = str((spec or {}).get("message") or ((spec or {}).get("story") or {}).get("claim") or (side or {}).get("message")
                  or facts.get("message") or "").strip()
    template = (spec or {}).get("template") or facts.get("template")
    medium = medium_of(o["kind"], template, Path(o["main"]))
    family = family_of(roles, o["kind"], spec)
    if family == "unknown" and facts.get("focus_fill"):
        family = "choropleth"
    prim, sec, source = infer_outcome(spec, roles, medium, message, family)
    if opts.get("outcome"):
        prim, sec, source = opts["outcome"], opts.get("secondary"), "option"
    style = (spec or {}).get("style") or {}
    return {"message": message or None, "outcome": prim, "secondary": sec, "outcome_source": source, "medium": medium, "family": family,
            "style_pack": (style.get("pack") if isinstance(style, dict) else None) or facts.get("style_pack") or "default",
            "look": (style.get("look") if isinstance(style, dict) else None) or "auto", "template": template,
            "scale_denominator": facts.get("scale_denominator"), "has_legend": facts.get("has_legend"),
            "branded": facts.get("branded", True), "data_layers": sum(1 for r in roles if r not in ("context", "contour", "hillshade")),
            "audience": (spec or {}).get("audience")}


# ---------------------------------------------------------------------------------------------------------------- applicability (pass 0)
def blended_weights(rub: dict, primary: str | None, secondary: str | None) -> dict:
    out = {}
    for a, w in rub["weights"].items():
        if primary is None:
            out[a] = 2.0                                     # unknown outcome: every area at a middle weight until the cold read says
            continue
        wp = float(w.get(primary, 0))
        if wp == 0:
            out[a] = 0.0
            continue
        ws = float(w.get(secondary, 0)) if secondary else wp
        out[a] = round((2 * wp + ws) / 3, 3)
    return out


def outcome_criteria_off(outcome: str | None) -> dict:
    """5.0.0: the criteria that are not scored for a map with this outcome, from the one applicability table the build and the draft
    readings also read (data/applicability.yaml). {criterion id: reason}."""
    try:
        import yaml
        doc = yaml.safe_load((HERE.parent / "data" / "applicability.yaml").read_text(encoding="utf-8")) or {}
        return dict(((doc.get("outcomes") or {}).get(str(outcome)) or {}).get("criteria_off") or {})
    except Exception:  # noqa: BLE001 - no table: every criterion the other rules leave on is scored
        return {}


def applicability(rub: dict, card: dict, frames: int) -> dict:
    """Pass 0 (spec 7.1): which criteria are scored and how much each area counts, fixed before any model sees the picture."""
    w = blended_weights(rub, card.get("outcome"), card.get("secondary"))
    na = []
    reasons = rub.get("not_applicable") or {}
    prim = card.get("outcome")
    den = card.get("scale_denominator")
    medium, family = card.get("medium"), card.get("family")

    def off(cid: str, why: str) -> None:
        na.append({"id": cid, "reason": why, "rule": reasons.get(cid, "")})

    if den and den >= 20_000_000:
        off("ORIENT-2", f"scale about 1:{den:,}: world or continental")
    by_outcome = outcome_criteria_off(prim)             # 5.0.0: what the map is for decides these, from one table (not from lines here)
    for cid, why in by_outcome.items():
        off(cid, f"a map to {prim}: {why}" if prim not in ("evoke",) else f"an evoke map: {why}")
    if card.get("has_legend") is False and card.get("data_layers", 0) <= 1 and "READ-2" not in by_outcome:
        off("READ-2", "no legend and a single data layer")
    if den and den <= 500_000:
        off("HON-3", f"scale about 1:{den:,}: city or metro, distortion negligible")
    if card.get("branded") is False:
        off("STYLE-5", "built with branding off")
    if frames <= 1:
        off("MEDIUM-3", "a single still")
    if medium not in ("social", "slide", "video"):
        off("READ-5", f"a {medium} output")
    if family == "terrain" and "HIER-2" not in by_outcome:
        off("HIER-2", "a terrain-only map")
    crit = criteria(rub)
    na_ids = {x["id"] for x in na}
    scored = [cid for cid, c in crit.items() if w.get(c["area"], 0) > 0 and cid not in na_ids]
    for a, v in w.items():
        if v == 0:
            na += [{"id": cid, "reason": f"area {a} not scored for a {prim} map", "rule": "weights"} for cid, c in crit.items() if c["area"] == a]
    return {"weights": w, "scored": scored, "na": na, "outcome": prim, "secondary": card.get("secondary")}


# ---------------------------------------------------------------------------------------------------------------- frames
def _pil():
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 200_000_000
    return Image


def _save_read_copy(im, dest: Path) -> Path:
    im = im.convert("RGB")
    s = READ_LONG_SIDE / max(im.size)
    if s < 1:
        im = im.resize((max(1, round(im.size[0] * s)), max(1, round(im.size[1] * s))), _pil().LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    im.save(dest, "PNG", optimize=True)
    return dest


def svg_png(svg_text: str, dpi: float) -> bytes:
    import map_export as mx
    return mx.rasterize(mx.to_pdf_bytes(svg_text, "evaluation"), dpi, svg_text)


def _dpi_for(svg_text: str, long_side: int) -> float:
    f = svg_facts(svg_text)
    W, H = f.get("sheet_mm") or (279.4, 215.9)
    return max(36.0, min(600.0, long_side / (max(W, H) / 25.4)))


def cue_times(video: Path) -> list[float]:
    """The beats of a video: the start of each caption cue in the .vtt or .srt beside it (2.0.0 writes them)."""
    out = []
    for ext in (".vtt", ".srt"):
        for cand in (video.with_suffix(ext), *video.parent.glob(video.stem + "*" + ext)):
            if cand.is_file():
                for m in re.finditer(r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->", cand.read_text(encoding="utf-8", errors="replace")):
                    h, mi, s, ms = (int(x) for x in m.groups())
                    out.append(h * 3600 + mi * 60 + s + ms / 1000)
                if out:
                    return sorted(set(round(t, 2) for t in out))
    return []


def _ffmpeg() -> str | None:
    try:
        import animate
        return animate.ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return shutil.which("ffmpeg")


def video_duration(p: Path) -> float | None:
    exe = _ffmpeg()
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-hide_banner", "-i", str(p)], capture_output=True, text=True, timeout=60)  # noqa: S603
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", r.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None


def video_times(p: Path) -> list[tuple[float, str]]:
    d = video_duration(p) or 0.0
    beats = cue_times(p)
    ts = {0.0: "first frame"}
    for b in beats:
        ts.setdefault(round(min(b + 0.5, max(0.0, d - 0.1)), 2), "beat")
    t = VIDEO_STEP_S
    while t < d - 0.5:
        ts.setdefault(round(t, 2), "every 5 s")
        t += VIDEO_STEP_S
    if d > 0.2:
        ts.setdefault(round(d - 0.1, 2), "last frame")
    items = sorted(ts.items())
    if len(items) > MAX_VIDEO_FRAMES:                         # keep first, last, the beats, then spread the rest
        keep = [i for i in items if i[1] != "every 5 s"]
        rest = [i for i in items if i[1] == "every 5 s"]
        room = max(0, MAX_VIDEO_FRAMES - len(keep))
        step = max(1, len(rest) // max(1, room)) if room else len(rest) + 1
        items = sorted((keep + rest[::step][:room])[:MAX_VIDEO_FRAMES])
    return items


def frames_for(o: dict, dest: Path) -> tuple[list[dict], list[dict]]:
    """The pictures a reader sees, as PNG files in `dest` (the cold folder). -> (frames, findings)."""
    Image = _pil()
    fs: list[dict] = []
    frames: list[dict] = []
    main = Path(o["main"])
    k = o["kind"]

    def add(im, label: str, **extra) -> None:
        p = _save_read_copy(im, dest / f"frame-{len(frames) + 1:02d}.png")
        frames.append({"file": p.name, "label": label, **extra})

    if k in ("still", "sequence"):
        members = [Path(m) for m in o.get("members") or [main]]
        for i, m in enumerate(members, 1):
            with Image.open(m) as im:
                add(im.copy(), f"panel {i} of {len(members)}" if len(members) > 1 else "the picture", source=m.name)
    elif k == "svg":
        text = main.read_text(encoding="utf-8", errors="replace")
        add(Image.open(io.BytesIO(svg_png(text, _dpi_for(text, READ_LONG_SIDE)))), "the sheet")
    elif k == "pdf":
        import pdf_stack
        rd = pdf_stack.read_pdf(main)
        n = len(rd.pages)
        pick = sorted({0, n - 1, *[round(i * (n - 1) / (MAX_PDF_PAGES - 1)) for i in range(MAX_PDF_PAGES)]})[:MAX_PDF_PAGES] if n > 1 else [0]
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(str(main))
        for i in pick:
            page = doc[i]
            w, h = page.get_size()
            scale = READ_LONG_SIDE / max(w, h)
            add(page.render(scale=scale).to_pil(), f"page {i + 1} of {n}" if n > 1 else "the sheet", page=i + 1)
        doc.close()
    elif k == "video":
        exe = _ffmpeg()
        if not exe:
            return [], [finding("MAP-EVAL-009", "WARN", f"{main.name}: ffmpeg is not available, so the video's frames cannot be read; run the map doctor")]
        for t, why in video_times(main):
            try:
                r = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(main), "-frames:v", "1", "-f", "image2pipe",  # noqa: S603
                                    "-vcodec", "png", "-"], capture_output=True, timeout=120)
                if r.returncode == 0 and r.stdout:
                    add(Image.open(io.BytesIO(r.stdout)), f"{why} at {t:.1f} s", t=t)
            except Exception as ex:  # noqa: BLE001
                fs.append(finding("MAP-EVAL-009", "WARN", f"{main.name}: the frame at {t:.1f} s could not be read ({type(ex).__name__})"))
    elif k == "web":
        try:
            from playwright.sync_api import sync_playwright
        except Exception:  # noqa: BLE001
            return [], [finding("MAP-EVAL-009", "WARN", f"{main.name}: Chromium (Playwright) is not available, so the web map's views cannot be taken")]
        try:
            with sync_playwright() as pw:
                b = pw.chromium.launch()
                for name, w, h in WEB_SIZES:
                    pg = b.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
                    pg.goto(main.resolve().as_uri(), wait_until="load", timeout=60000)
                    pg.wait_for_timeout(2500)
                    add(Image.open(io.BytesIO(pg.screenshot())), f"{name} {w}x{h}, default view", view=name)
                    btn = pg.query_selector("#gm-collapse") or pg.query_selector("[aria-controls=gm-panel]")
                    if btn:
                        try:
                            btn.click(timeout=3000)
                            pg.wait_for_timeout(800)
                            add(Image.open(io.BytesIO(pg.screenshot())), f"{name} {w}x{h}, panel toggled", view=name)
                        except Exception:  # noqa: BLE001
                            pass
                    pg.close()
                b.close()
        except Exception as ex:  # noqa: BLE001
            return frames, fs + [finding("MAP-EVAL-009", "WARN", f"{main.name}: the web map could not be opened in Chromium ({type(ex).__name__}: {str(ex)[:120]})")]
    return frames, fs


# ---------------------------------------------------------------------------------------------------------------- measurements (M-01 to M-12)
def _np():
    import numpy as np
    return np


def _rgb(im):
    np = _np()
    return np.asarray(im.convert("RGB"), dtype=np.float64) / 255.0


def _lab(rgb):
    np = _np()
    lin = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    M = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.1191920, 0.9503041]])
    xyz = lin @ M.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def _lum(rgb):
    np = _np()
    lin = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    return lin @ np.array([0.2126, 0.7152, 0.0722])


def _de(a, b):
    np = _np()
    return np.sqrt(((a - b) ** 2).sum(axis=-1))


def _shrink(im, long_side: int):
    s = long_side / max(im.size)
    if s >= 1:
        return im.convert("RGB")
    return im.convert("RGB").resize((max(1, round(im.size[0] * s)), max(1, round(im.size[1] * s))), _pil().BILINEAR)


def _background(lab):
    """The paper: the most common colour (quantized), as Lab."""
    np = _np()
    q = np.round(lab / 4).reshape(-1, 3)
    vals, counts = np.unique(q, axis=0, return_counts=True)
    return vals[counts.argmax()] * 4


def _blur(arr, radius: float):
    np = _np()
    from PIL import Image, ImageFilter
    a = arr.astype(np.float64)
    lo, hi = float(a.min()), float(a.max())
    if hi - lo < 1e-12:
        return a
    im = Image.fromarray(np.uint8(255 * (a - lo) / (hi - lo)))
    return np.asarray(im.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float64) / 255 * (hi - lo) + lo


def components(mask, max_components: int = 4000) -> list[tuple[int, int, int, int, int]]:
    """Connected regions of a boolean mask (8-neighbour): [(x0, y0, x1, y1, pixels)]. scipy when it is there, else a flood fill."""
    np = _np()
    try:
        from scipy import ndimage
        lab, n = ndimage.label(mask, structure=np.ones((3, 3)))
        out = []
        for i, sl in enumerate(ndimage.find_objects(lab)[:max_components], 1):
            if sl is None:
                continue
            px = int((lab[sl] == i).sum())
            out.append((sl[1].start, sl[0].start, sl[1].stop, sl[0].stop, px))
        return out
    except Exception:  # noqa: BLE001 - scipy is not a dependency of the plugin
        pass
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    out = []
    ys, xs = np.nonzero(mask)
    for y0, x0 in zip(ys.tolist(), xs.tolist(), strict=True):
        if seen[y0, x0]:
            continue
        stack = [(y0, x0)]
        seen[y0, x0] = True
        mnx = mxx = x0
        mny = mxy = y0
        n = 0
        while stack:
            y, x = stack.pop()
            n += 1
            mnx, mxx, mny, mxy = min(mnx, x), max(mxx, x), min(mny, y), max(mxy, y)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    yy, xx = y + dy, x + dx
                    if 0 <= yy < h and 0 <= xx < w and mask[yy, xx] and not seen[yy, xx]:
                        seen[yy, xx] = True
                        stack.append((yy, xx))
        out.append((mnx, mny, mxx + 1, mxy + 1, n))
        if len(out) >= max_components:
            break
    return out


def saliency(rgb):
    """M-01: spectral-residual saliency (Hou and Zhang 2007) on a 64-px-wide grey copy, blurred, scaled back to the picture, 0..1."""
    np = _np()
    from PIL import Image
    g = (rgb @ np.array([0.299, 0.587, 0.114]))
    h, w = g.shape
    small_w = 64
    small_h = max(8, round(h * small_w / w))
    s = np.asarray(Image.fromarray(np.uint8(g * 255)).resize((small_w, small_h), Image.BILINEAR), dtype=np.float64) / 255
    F = np.fft.fft2(s)
    logamp = np.log(np.abs(F) + 1e-9)
    phase = np.angle(F)
    k = np.ones((3, 3)) / 9
    pad = np.pad(logamp, 1, mode="edge")
    avg = sum(pad[i:i + logamp.shape[0], j:j + logamp.shape[1]] * k[i, j] for i in range(3) for j in range(3))
    sal = np.abs(np.fft.ifft2(np.exp(logamp - avg + 1j * phase))) ** 2
    sal = _blur(sal, 2.5)
    sal = (sal - sal.min()) / (sal.max() - sal.min() + 1e-12)
    big = np.asarray(Image.fromarray(np.uint8(sal * 255)).resize((w, h), Image.BILINEAR), dtype=np.float64) / 255
    return big


def _pct_box(b, w, h) -> list[float]:
    x0, y0, x1, y1 = b[:4]
    return [round(100 * x0 / w, 1), round(100 * y0 / h, 1), round(100 * x1 / w, 1), round(100 * y1 / h, 1)]


def top_regions(sal, frac: float = 0.05, n: int = 3) -> list[dict]:
    np = _np()
    h, w = sal.shape
    step = max(1, w // 160)
    s = sal[::step, ::step]
    thr = np.quantile(s, 1 - frac)
    comps = components(s >= thr)
    mass = []
    for x0, y0, x1, y1, _px in comps:
        mass.append((float(s[y0:y1, x0:x1][s[y0:y1, x0:x1] >= thr].sum()), (x0 * step, y0 * step, x1 * step, y1 * step)))
    mass.sort(key=lambda t: -t[0])
    tot = sum(m for m, _ in mass) or 1.0
    return [{"box": _pct_box(b, w, h), "share": round(m / tot, 3)} for m, b in mass[:n]]


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def picture_measures(im) -> dict:
    """Everything a plain picture gives: M-01, M-03, M-04, M-08, M-10 (picture edges), M-11 (attention stability)."""
    np = _np()
    im = _shrink(im, MEASURE_LONG_SIDE)
    rgb = _rgb(im)
    lab = _lab(rgb)
    h, w = lab.shape[:2]
    bg = _background(lab)
    ink = _de(lab, bg)
    inkm = ink > 6
    sal = saliency(rgb)
    out: dict = {"size_px": [w, h]}
    out["M-01"] = {"top_regions": top_regions(sal), "by_third": [[round(float(sal[r * h // 3:(r + 1) * h // 3, c * w // 3:(c + 1) * w // 3].mean() / (sal.mean() + 1e-12)), 2)
                                                                     for c in range(3)] for r in range(3)]}
    # M-03 clutter: colour variance and edge density per tile on a 400-px copy
    sm = _shrink(im, 400)
    sl = _lab(_rgb(sm))
    gy, gx = np.gradient(sl[..., 0])
    edges = np.hypot(gx, gy) > 8
    th, tw = max(1, sl.shape[0] // 8), max(1, sl.shape[1] // 8)
    tiles = []
    for r in range(8):
        for c in range(8):
            t = sl[r * th:(r + 1) * th, c * tw:(c + 1) * tw].reshape(-1, 3)
            e = edges[r * th:(r + 1) * th, c * tw:(c + 1) * tw]
            if t.size:
                tiles.append((float(np.sqrt(t.var(axis=0).sum())), float(e.mean()), r, c))
    cong = [t[0] for t in tiles]
    dens = [t[1] for t in tiles]
    busiest = max(tiles, key=lambda t: t[0] * (0.5 + t[1]))
    out["M-03"] = {"congestion_mean": round(statistics.fmean(cong), 1), "congestion_p90": round(float(np.quantile(cong, 0.9)), 1),
                   "edge_density": round(float(edges.mean()), 3), "edge_density_p90": round(float(np.quantile(dens, 0.9)), 3),
                   "busiest_tile_pct": [round(100 * busiest[3] / 8, 1), round(100 * busiest[2] / 8, 1), round(100 * (busiest[3] + 1) / 8, 1), round(100 * (busiest[2] + 1) / 8, 1)]}
    # M-04 balance and white space
    wgt = ink * inkm
    tot = float(wgt.sum()) or 1.0
    ys, xs = np.mgrid[0:h, 0:w]
    cx, cy = float((wgt * xs).sum() / tot), float((wgt * ys).sum() / tot)
    quads = [float(inkm[:h // 2, :w // 2].mean()), float(inkm[:h // 2, w // 2:].mean()), float(inkm[h // 2:, :w // 2].mean()), float(inkm[h // 2:, w // 2:].mean())]
    out["M-04"] = {"centre_offset_pct": [round(100 * (cx - w / 2) / (w / 2), 1), round(100 * (cy - h / 2) / (h / 2), 1)],
                   "white_space": round(1 - float(inkm.mean()), 3), "ink_by_quadrant": [round(q, 3) for q in quads]}
    # M-08 colour count: distinct hues over 1 percent of the frame, and the neutral share
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    hue = (np.degrees(np.arctan2(lab[..., 2], lab[..., 1])) + 360) % 360
    colourful = chroma > 15
    bins = np.bincount((hue[colourful] // 15).astype(int), minlength=24) / (h * w)
    out["M-08"] = {"hues_over_1pct": int((bins > 0.01).sum()), "neutral_share": round(1 - float(colourful.mean()), 3)}
    # M-10 picture edge: ink in the outer 1 percent band, per side
    bw = max(1, round(0.01 * min(w, h)))
    sides = {"top": inkm[:bw, :], "bottom": inkm[-bw:, :], "left": inkm[:, :bw], "right": inkm[:, -bw:]}
    out["M-10"] = {"edge_ink": {k: round(float(v.mean()), 3) for k, v in sides.items()}}
    # M-11 thumbnail: does the attention land in the same place at feed size?
    thumb = _shrink(im, THUMB_WIDTH)
    tsal = saliency(_rgb(thumb))
    a = top_regions(sal, n=1)
    b = top_regions(tsal, n=1)
    out["M-11"] = {"attention_iou": round(_iou(a[0]["box"], b[0]["box"]), 2) if a and b else None, "thumb_px": list(thumb.size)}
    out["_ink_mask"] = inkm
    out["_sal"] = sal
    return out


def _variant(svg_text: str, drop: set[str], drop_text: bool = False) -> str:
    """The SVG with some kinds of group removed: data, context, furniture (see FURNITURE / CONTEXT), or all text."""
    from lxml import etree
    import map_preview as pv
    root = etree.fromstring(svg_text.encode("utf-8"), pv._parser())
    ns = "{http://www.w3.org/2000/svg}"
    kill = []
    for g in root.iter(f"{ns}g", f"{ns}svg", f"{ns}image", f"{ns}rect"):
        cls = (g.get("class") or "").split()
        gid = g.get("id") or ""
        kind = None
        if any(c.startswith(FURNITURE) for c in cls):
            kind = "furniture"
        elif any(c.startswith(CONTEXT) for c in cls) or (gid.startswith("gm-layer-") and g.get("data-role") in ("context", "contour")):
            kind = "context"
        elif gid.startswith("gm-layer-") or any(c.startswith(DATA_TEXT) for c in cls):
            kind = "data"
        if kind in drop:
            kill.append(g)
    if drop_text:
        kill += list(root.iter(f"{ns}text"))
    for el in kill:
        p = el.getparent()
        if p is not None:
            p.remove(el)
    return etree.tostring(root, encoding="unicode")


def text_sizes(svg_text: str) -> dict:
    """M-05 from the SVG: every <text> size in points at the sheet's printed size (halo copies skipped)."""
    f = svg_facts(svg_text)
    unit = f.get("unit_mm") or 1.0
    sizes = []
    for m in re.finditer(r"<text\b([^>]*)>", svg_text):
        attrs = m.group(1)
        if re.search(r'\bstroke="(?!none)', attrs) and "data-name" not in attrs and 'class="gm-t"' in attrs and "fill=" in attrs:
            st = re.search(r'stroke="([^"]+)"', attrs)
            fl = re.search(r'fill="([^"]+)"', attrs)
            if st and fl and st.group(1) == fl.group(1):          # the halo copy drawn under the text
                continue
        fsz = re.search(r'font-size[=:]\s*"?([\d.]+)', attrs)
        if fsz:
            sizes.append(float(fsz.group(1)) * unit * 72 / 25.4)
    if not sizes:
        return {"items": 0}
    sizes.sort()
    return {"items": len(sizes), "min_pt": round(sizes[0], 1), "p10_pt": round(sizes[max(0, len(sizes) // 10 - 1)], 1), "median_pt": round(statistics.median(sizes), 1),
            "below_6pt": sum(1 for s in sizes if s < 6.0), "below_7pt": sum(1 for s in sizes if s < 7.0)}


def legend_colours(svg_text: str) -> list[str]:
    m = re.search(r'<g class="gm-legend".*?(?=<g class="gm-(?!legend)|\Z)', svg_text, re.S)
    if not m:
        return []
    block = m.group(0)
    fills = re.findall(r'<(?:rect|path|circle|polygon)\b[^>]*\bfill="(#[0-9A-Fa-f]{6})"', block)
    if not fills:
        return []
    panel = fills[0]
    out = []
    for c in fills[1:]:
        c = c.upper()
        if c != panel.upper() and c not in out:
            out.append(c)
    return out[:24]


def class_distinctness(colours: list[str]) -> dict:
    import colorlib as cl
    if len(colours) < 2:
        return {"swatches": len(colours)}
    out = {"swatches": len(colours)}
    for cvd in (None, "deutan", "protan", "tritan"):
        best = min(cl.delta_e(a, b, cvd) for i, a in enumerate(colours) for b in colours[i + 1:])
        out["min_de_" + (cvd or "normal")] = round(best, 1)
    Ls = sorted(cl.lab(c)[0] for c in colours)
    out["min_dl_grey"] = round(min(b - a for a, b in zip(Ls, Ls[1:], strict=False)), 1)
    return out


def structure_measures(svg_text: str, pic: dict | None = None) -> tuple[dict, list[dict]]:
    """What the plugin's own SVG adds: M-02 data share, M-05 text sizes, M-06 text contrast, M-07 class distinctness, M-09 furniture,
    M-10 data at the map frame's edge. Each from renders of the sheet with groups taken out; a render that fails leaves its value out."""
    np = _np()
    Image = _pil()
    out: dict = {}
    fs: list[dict] = []
    facts = svg_facts(svg_text)
    out["M-05"] = text_sizes(svg_text)
    out["M-07"] = class_distinctness(legend_colours(svg_text))
    try:
        def lab_of(svg, dpi):
            return _lab(_rgb(Image.open(io.BytesIO(svg_png(svg, dpi)))))
        blank = lab_of(_variant(svg_text, {"data", "context", "furniture"}, drop_text=True), MASK_DPI)
        data = lab_of(_variant(svg_text, {"context", "furniture"}), MASK_DPI)
        ctx = lab_of(_variant(svg_text, {"data", "furniture"}), MASK_DPI)
        furn = lab_of(_variant(svg_text, {"data", "context"}), MASK_DPI)
        dd, dc, df = _de(data, blank), _de(ctx, blank), _de(furn, blank)
        wd, wc = float(dd[dd > 2].sum()), float(dc[dc > 2].sum())
        out["M-02"] = {"data_share_of_weight": round(wd / (wd + wc), 3) if wd + wc else None}
        dmask = dd > 3
        fmask = df > 3
        h, w = dmask.shape
        if pic and pic.get("_sal") is not None:
            sal = np.asarray(Image.fromarray(np.uint8(255 * pic["_sal"])).resize((w, h), Image.BILINEAR), dtype=np.float64) / 255
            out["M-02"]["attention_on_data"] = round(float(sal[dmask].sum() / (sal.sum() + 1e-12)), 3)
            out["M-02"]["attention_on_furniture"] = round(float(sal[fmask].sum() / (sal.sum() + 1e-12)), 3)
        focus = _dilate(dmask, max(1, round(0.02 * w)))
        out["M-09"] = {"furniture_share_of_frame": round(float(fmask.mean()), 3),
                       "focus_covered_by_furniture": round(float((fmask & focus).sum() / max(1, focus.sum())), 3)}
        fr = facts.get("map_frame_pct")
        if fr:
            x0, y0, x1, y1 = (round(fr[0] * w / 100), round(fr[1] * h / 100), round(fr[2] * w / 100), round(fr[3] * h / 100))
            band = max(1, round(0.01 * min(w, h)))
            inner = dmask[y0:y1, x0:x1]
            tot = max(1, int(inner.sum()))
            out["M-10"] = {"data_at_frame_edge": {"top": round(float(inner[:band].sum()) / tot, 3), "bottom": round(float(inner[-band:].sum()) / tot, 3),
                                                  "left": round(float(inner[:, :band].sum()) / tot, 3), "right": round(float(inner[:, -band:].sum()) / tot, 3)}}
    except Exception as ex:  # noqa: BLE001
        fs.append(finding("MAP-EVAL-009", "WARN", f"the layer renders for M-02, M-09 and M-10 failed ({type(ex).__name__}: {str(ex)[:120]})"))
    try:
        full = _rgb(Image.open(io.BytesIO(svg_png(svg_text, TEXT_DPI))))
        notext = _rgb(Image.open(io.BytesIO(svg_png(_variant(svg_text, set(), drop_text=True), TEXT_DPI))))
        tmask = _de(_lab(full), _lab(notext)) > 10
        lum = _lum(full)
        ratios, heights = [], []
        line = _dilate_h(tmask, max(2, round(TEXT_DPI / 25.4 * 1.2)))
        min_h = 0.9 * TEXT_DPI / 25.4                       # a line of text is at least 0.9 mm tall; smaller pieces are rule ends and anti-aliasing
        for x0, y0, x1, y1, px in components(line):
            if px < 30 or (y1 - y0) < min_h:
                continue
            reg = lum[max(0, y0 - 1):y1 + 1, max(0, x0 - 1):x1 + 1]
            hi, lo = float(np.quantile(reg, 0.9)), float(np.quantile(reg, 0.1))
            ratios.append((hi + 0.05) / (lo + 0.05))
            heights.append((y1 - y0) / TEXT_DPI * 25.4)
        if ratios:
            r = sorted(ratios)
            out["M-06"] = {"text_items": len(r), "min_ratio": round(r[0], 2), "p10_ratio": round(r[max(0, len(r) // 10 - 1)], 2),
                           "share_below_3": round(sum(1 for x in r if x < 3) / len(r), 3), "share_below_4_5": round(sum(1 for x in r if x < 4.5) / len(r), 3)}
            if pic:
                scale = THUMB_WIDTH / (facts.get("sheet_mm") or [279.4])[0] * 1.0     # px per mm at feed size
                legible = sum(1 for hmm in heights if hmm * scale >= 9) / len(heights)
                out.setdefault("M-11", {})["text_lines_legible_at_thumb"] = round(legible, 3)
    except Exception as ex:  # noqa: BLE001
        fs.append(finding("MAP-EVAL-009", "WARN", f"the text renders for M-06 failed ({type(ex).__name__}: {str(ex)[:120]})"))
    return out, fs


def _dilate(mask, r: int):
    from PIL import Image, ImageFilter
    np = _np()
    im = Image.fromarray(np.uint8(mask) * 255).filter(ImageFilter.MaxFilter(2 * min(r, 12) + 1))
    return np.asarray(im) > 0


def _dilate_h(mask, r: int):
    out = mask.copy()
    for d in range(1, r + 1):
        out[:, d:] |= mask[:, :-d]
        out[:, :-d] |= mask[:, d:]
    return out


def sequence_measures(frame_paths: list[Path]) -> dict:
    """M-12: how much the outer band (where furniture sits) and the colour mix change from frame to frame."""
    np = _np()
    Image = _pil()
    if len(frame_paths) < 2:
        return {}
    prev = None
    band_d, hist_d = [], []
    for p in frame_paths:
        with Image.open(p) as im:
            lab = _lab(_rgb(_shrink(im, 400)))
        h, w = lab.shape[:2]
        b = max(1, round(0.12 * min(h, w)))
        ring = np.concatenate([lab[:b].reshape(-1, 3), lab[-b:].reshape(-1, 3), lab[:, :b].reshape(-1, 3), lab[:, -b:].reshape(-1, 3)])
        hist = np.histogram(lab[..., 0], bins=16, range=(0, 100))[0] / (h * w)
        if prev is not None and prev[0].shape == ring.shape:
            band_d.append(float(_de(ring, prev[0]).mean()))
            hist_d.append(float(np.abs(hist - prev[1]).sum() / 2))
        prev = (ring, hist)
    if not band_d:
        return {"M-12": {"note": "frames differ in size; not compared"}}
    return {"M-12": {"furniture_band_change_mean": round(statistics.fmean(band_d), 2), "furniture_band_change_max": round(max(band_d), 2),
                     "colour_mix_change_max": round(max(hist_d), 3)}}


def measure(picture: Path, svg: Path | None = None) -> tuple[dict, list[dict]]:
    """M-01 to M-11 for one picture (plus the SVG's structure when it is the plugin's own). -> (measures, findings)."""
    Image = _pil()
    with Image.open(picture) as im:
        pic = picture_measures(im.copy())
    fs = []
    if svg is not None:
        try:
            st, fs = structure_measures(Path(svg).read_text(encoding="utf-8", errors="replace"), pic)
            for k, v in st.items():
                pic[k] = {**pic.get(k, {}), **v} if isinstance(v, dict) and isinstance(pic.get(k), dict) else v
        except Exception as ex:  # noqa: BLE001
            fs.append(finding("MAP-EVAL-009", "WARN", f"the SVG's structure could not be measured ({type(ex).__name__})"))
    pic.pop("_ink_mask", None)
    pic.pop("_sal", None)
    pic["measure_version"] = MEASURE_VERSION
    pic["structure"] = svg is not None
    return pic, fs


def measures_text(m: dict) -> list[str]:
    """The measurements as plain lines for the graded read. They inform; they never set a score."""
    L = []
    g = lambda k, d=None: (m.get(k) or {}) if d is None else (m.get(k) or {}).get(d)  # noqa: E731
    if g("M-01"):
        L.append("M-01 attention: top regions (box in % of frame x0,y0,x1,y1 and share) " + "; ".join(f"{r['box']} {round(100 * r['share'])}%" for r in g("M-01")["top_regions"]))
    if g("M-02"):
        L.append(f"M-02 data share of visual weight {g('M-02', 'data_share_of_weight')}; attention on data {g('M-02', 'attention_on_data')}, on furniture {g('M-02', 'attention_on_furniture')}")
    if g("M-03"):
        L.append(f"M-03 clutter: congestion mean {g('M-03', 'congestion_mean')} (p90 {g('M-03', 'congestion_p90')}), edge density {g('M-03', 'edge_density')}; busiest tile {g('M-03', 'busiest_tile_pct')}")
    if g("M-04"):
        L.append(f"M-04 balance: centre of visual weight offset {g('M-04', 'centre_offset_pct')} (% of half-frame, x right, y down); white space {g('M-04', 'white_space')}")
    if g("M-05") and g("M-05", "items"):
        L.append(f"M-05 text at printed size: {g('M-05', 'items')} items, smallest {g('M-05', 'min_pt')} pt, median {g('M-05', 'median_pt')} pt, {g('M-05', 'below_6pt')} below 6 pt")
    if g("M-06"):
        L.append(f"M-06 text contrast: lowest {g('M-06', 'min_ratio')}:1, p10 {g('M-06', 'p10_ratio')}:1, share below 3:1 {g('M-06', 'share_below_3')}, below 4.5:1 {g('M-06', 'share_below_4_5')}")
    if g("M-07") and g("M-07", "swatches", ) and g("M-07", "swatches") > 1:
        x = g("M-07")
        L.append(f"M-07 legend swatches {x['swatches']}: smallest delta E normal {x.get('min_de_normal')}, deutan {x.get('min_de_deutan')}, protan {x.get('min_de_protan')}, "
                 f"tritan {x.get('min_de_tritan')}; smallest lightness step {x.get('min_dl_grey')}")
    if g("M-08"):
        L.append(f"M-08 colours: {g('M-08', 'hues_over_1pct')} hues over 1% of the frame; neutral share {g('M-08', 'neutral_share')}")
    if g("M-09"):
        L.append(f"M-09 furniture covers {g('M-09', 'furniture_share_of_frame')} of the frame and {g('M-09', 'focus_covered_by_furniture')} of the data's focus area")
    if g("M-10"):
        x = g("M-10")
        if x.get("data_at_frame_edge"):
            L.append(f"M-10 data at the map frame's edge (share of data ink in the outer 1% band): {x['data_at_frame_edge']}")
        else:
            L.append(f"M-10 ink at the picture's edge: {x.get('edge_ink')}")
    if g("M-11"):
        L.append(f"M-11 thumbnail ({THUMB_WIDTH} px wide): attention overlap with full size {g('M-11', 'attention_iou')}"
                 + (f"; text lines legible {g('M-11', 'text_lines_legible_at_thumb')}" if g("M-11", "text_lines_legible_at_thumb") is not None else ""))
    if g("M-12"):
        L.append(f"M-12 frame to frame: {g('M-12')}")
    return L


# ---------------------------------------------------------------------------------------------------------------- packets and prompts
def packet_for(o: dict, run: Path, rub: dict, opts: dict) -> tuple[dict, list[dict]]:
    od = run / o["id"]
    cold = od / "cold"
    shutil.rmtree(od, ignore_errors=True)
    cold.mkdir(parents=True)
    (od / "reads").mkdir()
    frames, fs = frames_for(o, cold)
    if not frames:
        return {"id": o["id"], "status": "skipped", "reason": (fs[0]["message"] if fs else "no frames")}, fs
    card = intent_card(o, opts)
    app = applicability(rub, card, len(frames))
    mfs = []
    per = []
    for i, fr in enumerate(frames):
        svg = Path(o["svg"]) if (o.get("svg") and i == 0 and o["kind"] in ("still", "pdf", "svg")) else (Path(o["main"]) if (o["kind"] == "svg" and i == 0) else None)
        try:
            m, f2 = measure(cold / fr["file"], svg)
        except Exception as ex:  # noqa: BLE001
            m, f2 = {"error": f"{type(ex).__name__}"}, [finding("MAP-EVAL-009", "WARN", f"{o['id']} frame {i + 1}: not measured ({type(ex).__name__}: {str(ex)[:100]})")]
        mfs += f2
        per.append(m)
    meas = {"frames": per, **sequence_measures([cold / f["file"] for f in frames])}
    if card["outcome"] is None:
        fs.append(finding("MAP-EVAL-007", "INFO", f"{o['id']}: nothing says what the map is for; the cold read's guess will set the outcome"))
    files_hash = {Path(f).name: _sha(Path(f)) for f in o["files"]}
    pk = {"id": o["id"], "kind": o["kind"], "files": [Path(f).name for f in o["files"]], "sha256": files_hash, "frames": frames,
          "intent": card, "applicability": app, "status": "packed", "passes": int(opts.get("passes") or 1)}
    (od / "packet.json").write_text(json.dumps(pk, indent=1, default=str), encoding="utf-8")
    (od / "measures.json").write_text(json.dumps(meas, indent=1, default=str), encoding="utf-8")
    (od / "prompt-cold.md").write_text(cold_prompt(od, frames), encoding="utf-8")
    return pk, fs + mfs


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


COLD_SCHEMA = '{"takeaway": "one sentence", "eye_path": ["first", "second", "third"], "struggles": "what you had to work at, and what you could not work out", "audience_guess": "who it is for and what they would do with it", "outcome_guess": "explain|compare|locate|inventory|monitor|explore|sequence|evoke"}'


def cold_prompt(od: Path, frames: list[dict]) -> str:
    files = "\n".join(f"- `{od / 'cold' / f['file']}` ({f['label']})" for f in frames)
    return f"""# Cold read of a map (map evaluator, pass 1)

You are seeing this map for the first time. You know nothing about who made it or why, and that is the point: you stand in for a stranger.

Look ONLY at these pictures. Do not open, list or search any other file or folder, and do not look for the map's data, its MapSpec or
anything written about it.

{files}

Answer five questions about what you see:

1. In one sentence, what is this map telling you?
2. Where did your eye go first, second and third?
3. What did you have to work to understand, and what could you not work out?
4. Who do you think this is for, and what would they do with it?
5. Which of these does it seem to be for: explain (make one point), compare (show a pattern across places), locate (help find things),
   inventory (record what is where), monitor (status at a glance), explore (invite digging in), sequence (a story over beats or time),
   evoke (an impression; the look is the point)?

Write your answer as JSON, and nothing else, to `{od / 'reads' / 'cold.json'}`:

```json
{COLD_SCHEMA}
```

If you cannot write files, reply with the JSON alone.
"""


GRADED_SCHEMA = """{"scores": [
  {"id": "STORY-1", "score": 4, "confidence": "high|medium|low",
   "evidence": {"frame": 1, "box": [x0, y0, x1, y1], "seen": "what is seen there"},
   "candidate": null},
  {"id": "READ-1", "score": 3, "confidence": "medium",
   "evidence": {"frame": 1, "box": [62, 4, 98, 30], "seen": "legend class labels about 5 pt, grey on pale fill"},
   "candidate": {"component": "label_faces", "text": "the label faces at this scale set legend text below 6 pt on a social sheet"}},
  {"id": "HON-3", "score": "na", "na_reason": "why this criterion cannot apply to this map"}
]}"""


def graded_prompt(od: Path, pk: dict, rub: dict, cold: dict, n: int) -> str:
    crit = criteria(rub)
    card = pk["intent"]
    app = pk["applicability"]
    meas = json.loads((od / "measures.json").read_text(encoding="utf-8"))
    frames = pk["frames"]
    files = "\n".join(f"- frame {i}: `{od / 'cold' / f['file']}` ({f['label']})" for i, f in enumerate(frames, 1))
    scale = "\n".join(f"- **{k}**: {v}" for k, v in sorted(rub["scale"].items(), key=lambda kv: -int(kv[0])))
    by_area: dict[str, list[str]] = {}
    for cid in app["scored"]:
        c = crit[cid]
        by_area.setdefault(c["area"], []).append(f"- **{cid} {c['name']}.** 5: {c[5]}. 3: {c[3]}. 1: {c[1]}." + (" *(experimental)*" if c.get("experimental") else ""))
    blocks = []
    for a in rub["areas"]:
        if a in by_area:
            blocks.append(f"### {a}: {rub['areas'][a]['name']} (weight {app['weights'][a]:g})\n{rub['areas'][a]['question']}\n\n" + "\n".join(by_area[a]))
    na = "\n".join(f"- {x['id']}: {x['reason']}" for x in app["na"] if not x["reason"].startswith("area ")) or "- none"
    comps = ", ".join(f"`{k}`" for k in rub["components"])
    mlines = []
    for i, fm in enumerate(meas.get("frames") or [], 1):
        if len(frames) > 1:
            mlines.append(f"Frame {i}:")
        mlines += measures_text(fm)
    if meas.get("M-12"):
        mlines.append(f"M-12 across frames: {meas['M-12']}")
    outcome = card.get("outcome") or cold.get("outcome_guess") or "unknown"
    return f"""# Graded read of a map (map evaluator, pass 2, read {n})

You grade one finished map against a rubric, from the picture. You did not make it. Your grades are kept as a record for improving the
tool that made it; they never change this map. Be exact and strict: the bar is high.

## The pictures
{files}

Look only at these pictures and the facts below. Do not open, list or search any other file or folder.

## What the map was for (the intent card)
- Message: {card.get('message') or '(none given)'}
- Outcome: {outcome}{(' (secondary: ' + card['secondary'] + ')') if card.get('secondary') else ''}{' (inferred)' if card.get('outcome_source') in ('inferred', 'unknown') else ''}
- Medium: {card.get('medium')} · family: {card.get('family')} · style pack: {card.get('style_pack')} (judge craft against this style, not against another)
- Template: {card.get('template') or '-'}

## What a stranger saw first (the cold read, word for word)
- Takeaway: {cold.get('takeaway')}
- Eye path: {', '.join(cold.get('eye_path') or [])}
- Struggles: {cold.get('struggles')}
- Audience guess: {cold.get('audience_guess')}

STORY-1 compares that takeaway with the message above: a match (subject and point), a partial match (subject right, point missed) or a miss.

## Measurements (computed from the pixels; they inform your judgement, they never set a score)
{chr(10).join('- ' + x for x in mlines) or '- none'}

## The scale
{scale}

{rub.get('bar', '')}

Rules that always hold:
- No criterion rewards having an element. A missing legend, inset or annotation costs points only when this reader needs it for this outcome.
- Unneeded elements cost points (INFO-3). A plain map that works can score 5 on effectiveness.
- Every score needs evidence: the frame, a box in percent of that frame (x0, y0, x1, y1, from the top left) and what is seen there. A score without evidence is void.
- For any score of 3 or less, give an improvement candidate: name one component from this list and say, in one sentence, what that part of
  the tool did here. Write it about the tool ("the label placer let place names collide with the legend"), never as an instruction for this
  map ("move the legend"), and never address anyone as "you". Components: {comps}.
- Score "na" only when a criterion truly cannot apply, with a reason; it is flagged for review.

## The criteria to score
{chr(10).join(blocks)}

Not scored for this map (already decided):
{na}

## Answer
Write JSON, and nothing else, to `{od / 'reads' / f'graded-{n}.json'}`, one entry per criterion above:

```json
{GRADED_SCHEMA}
```

If you cannot write files, reply with the JSON alone.
"""


# ---------------------------------------------------------------------------------------------------------------- the run's state
def load_run(run: Path) -> dict:
    p = run / "evaluation.json"
    if not p.is_file():
        raise EvalError(f"{run.name} is not an evaluation run (no evaluation.json)", "MAP-EVAL-005")
    return json.loads(p.read_text(encoding="utf-8"))


def read_json_file(p: Path):
    text = p.read_text(encoding="utf-8", errors="replace").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    return json.loads(text)


def status(run: Path) -> dict:
    """What is still needed. A missing graded prompt is written as soon as its cold read is in."""
    ev = load_run(run)
    rub = load_rubric(run / "rubric.yaml")
    pending, done = [], []
    for oid in ev["outputs"]:
        od = run / oid
        pk = json.loads((od / "packet.json").read_text(encoding="utf-8"))
        if pk.get("status") == "skipped":
            continue
        cold_p = od / "reads" / "cold.json"
        if not cold_p.is_file():
            pending.append({"output": oid, "read": f"{oid}:cold", "prompt": str(od / "prompt-cold.md"), "answer": str(cold_p)})
            continue
        try:
            cold = validate_cold(read_json_file(cold_p))
        except Exception as ex:  # noqa: BLE001
            pending.append({"output": oid, "read": f"{oid}:cold", "prompt": str(od / "prompt-cold.md"), "answer": str(cold_p), "problem": f"the cold read cannot be used: {ex}"})
            continue
        done.append(f"{oid}:cold")
        for n in range(1, int(pk.get("passes") or 1) + 1):
            gp = od / f"prompt-graded-{n}.md"
            if not gp.is_file():
                gp.write_text(graded_prompt(od, pk, rub, cold, n), encoding="utf-8")
            ans = od / "reads" / f"graded-{n}.json"
            if ans.is_file():
                done.append(f"{oid}:graded-{n}")
            else:
                pending.append({"output": oid, "read": f"{oid}:graded-{n}", "prompt": str(gp), "answer": str(ans)})
    return {"run": str(run), "pending": pending, "done": done, "ready": not pending}


def save_read(run: Path, read_id: str, src: Path) -> Path:
    oid, _, which = read_id.partition(":")
    if not which or not (run / oid / "packet.json").is_file():
        raise EvalError(f"'{read_id}' is not a read of this run (use the ids `status` prints)", "MAP-EVAL-005")
    data = read_json_file(src)
    dest = run / oid / "reads" / (which + ".json")
    dest.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return dest


OUTCOMES = ("explain", "compare", "locate", "inventory", "monitor", "explore", "sequence", "evoke")


def validate_cold(d) -> dict:
    if not isinstance(d, dict) or not str(d.get("takeaway") or "").strip():
        raise ValueError("it needs at least a one-sentence takeaway")
    og = str(d.get("outcome_guess") or "").strip().lower()
    d["outcome_guess"] = og if og in OUTCOMES else None
    d["eye_path"] = [str(x) for x in (d.get("eye_path") or [])][:3]
    return {k: d.get(k) for k in ("takeaway", "eye_path", "struggles", "audience_guess", "outcome_guess")}


def check_candidate(c, comps: set) -> tuple[dict | None, list[str]]:
    """I6: a candidate is about the tool, never an edit of this map. -> (candidate or None, problems)."""
    if not c:
        return None, []
    if isinstance(c, str):
        c = {"text": c, "component": None}
    text = str(c.get("text") or "").strip()
    comp = c.get("component")
    probs = []
    if not text:
        return None, ["empty"]
    if IMPERATIVE.search(text) or SECOND_PERSON.search(text) or THIS_MAP.search(text):
        probs.append("worded as an edit of this map, not a finding about the tool")
    if comp not in comps:
        probs.append(f"unknown component {comp!r}")
        comp = "unassigned"
    return {"component": comp, "text": text[:400], "refused": bool(probs and "worded" in probs[0])}, probs


def validate_graded(d, scored: list[str], crit: dict, comps: set, nframes: int, oid: str, n: int) -> tuple[dict, list[dict]]:
    """One graded read -> ({criterion: entry}, findings). Unknown ids, scores out of range and missing evidence are void, never guessed."""
    fs = []
    rows = d.get("scores") if isinstance(d, dict) else d
    if not isinstance(rows, list):
        raise ValueError("it needs a 'scores' list")
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        cid = str(r.get("id") or "").upper()
        if cid not in crit:
            fs.append(finding("MAP-EVAL-003", "WARN", f"{oid} read {n}: '{cid}' is not a criterion of this rubric; ignored"))
            continue
        if cid not in scored:
            fs.append(finding("MAP-EVAL-003", "INFO", f"{oid} read {n}: {cid} was not to be scored for this map; ignored"))
            continue
        sc = r.get("score")
        if isinstance(sc, str) and sc.strip().lower() in ("na", "n/a"):
            reason = str(r.get("na_reason") or "").strip()
            if reason:
                out[cid] = {"na": True, "reason": reason[:300]}
                fs.append(finding("MAP-EVAL-004", "INFO", f"{oid} read {n}: the grader proposed {cid} not applicable: {reason[:160]}"))
            else:
                out[cid] = {"void": "n/a without a reason"}
                fs.append(finding("MAP-EVAL-001", "WARN", f"{oid} read {n}: {cid} n/a without a reason; void"))
            continue
        try:
            sc = int(round(float(sc)))
        except (TypeError, ValueError):
            sc = None
        if sc is None or not 1 <= sc <= 5:
            out[cid] = {"void": "score outside 1-5"}
            fs.append(finding("MAP-EVAL-003", "WARN", f"{oid} read {n}: {cid} score {r.get('score')!r} is outside 1-5; void"))
            continue
        ev = r.get("evidence") or {}
        box = ev.get("box") if isinstance(ev, dict) else None
        seen = str(ev.get("seen") or "").strip() if isinstance(ev, dict) else ""
        ok_box = isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box) and 0 <= box[0] < box[2] <= 100 and 0 <= box[1] < box[3] <= 100
        if not ok_box or len(seen) < 8:
            out[cid] = {"void": "no evidence"}
            fs.append(finding("MAP-EVAL-001", "WARN", f"{oid} read {n}: {cid} has no usable evidence (a box in percent and what is seen); void"))
            continue
        frame = ev.get("frame") or 1
        try:
            frame = max(1, min(nframes, int(frame)))
        except (TypeError, ValueError):
            frame = 1
        cand, probs = check_candidate(r.get("candidate"), comps)
        for p in probs:
            fs.append(finding("MAP-EVAL-002", "WARN", f"{oid} read {n}: {cid} improvement candidate {p}", (cand or {}).get("text")))
        conf = str(r.get("confidence") or "medium").lower()
        out[cid] = {"score": sc, "confidence": conf if conf in ("high", "medium", "low") else "medium",
                    "evidence": {"frame": frame, "box": [round(float(v), 1) for v in box], "seen": seen[:400]}, "candidate": cand if cand and not cand["refused"] else None,
                    "refused_candidate": cand["text"] if cand and cand["refused"] else None}
    for cid in scored:
        if cid not in out:
            out[cid] = {"void": "not answered"}
            fs.append(finding("MAP-EVAL-001", "WARN", f"{oid} read {n}: {cid} was not answered; unscored"))
    return out, fs


# ---------------------------------------------------------------------------------------------------------------- scoring
def half(x: float) -> float:
    return math.floor(x * 2 + 0.5) / 2


def combine(reads: list[dict], scored: list[str]) -> dict:
    """Several graded reads -> one entry per criterion: the median score, its spread, the evidence of the read nearest the median."""
    out = {}
    for cid in scored:
        rows = [r.get(cid) or {"void": "not answered"} for r in reads]
        vals = [r for r in rows if "score" in r]
        nas = [r for r in rows if r.get("na")]
        if not vals:
            out[cid] = {"na": True, "reason": nas[0]["reason"], "by": "grader"} if nas and len(nas) * 2 > len(rows) else {"unscored": (rows[0].get("void") or "void")}
            continue
        scores = sorted(v["score"] for v in vals)
        med = statistics.median(scores)
        pick = min(vals, key=lambda v: abs(v["score"] - med))
        confs = [v["confidence"] for v in vals]
        cands = []
        for v in vals:
            if v.get("candidate") and v["score"] <= 3 and all(c["text"] != v["candidate"]["text"] for c in cands):
                cands.append(v["candidate"])
        out[cid] = {"score": med if len(vals) > 1 else vals[0]["score"], "scores": scores, "spread": scores[-1] - scores[0],
                    "confidence": max(set(confs), key=confs.count), "evidence": pick["evidence"], "candidates": cands,
                    "refused_candidates": [v["refused_candidate"] for v in vals if v.get("refused_candidate")]}
    return out


def score_card(rub: dict, pk: dict, cold: dict, entries: dict) -> dict:
    crit = criteria(rub)
    app = pk["applicability"]
    weights = dict(app["weights"])
    outcome = pk["intent"].get("outcome")
    flags = []
    if outcome is None and cold.get("outcome_guess"):                    # MAP-EVAL-007: the cold read sets the outcome
        outcome = cold["outcome_guess"]
        weights = blended_weights(rub, outcome, None)
        flags.append("outcome_from_cold_read")
    elif pk["intent"].get("outcome_source") in ("inferred", "unknown"):
        flags.append("outcome_inferred")
    areas = {}
    for a in rub["areas"]:
        vals = [e["score"] for cid, e in entries.items() if crit[cid]["area"] == a and "score" in e and not crit[cid].get("experimental")]
        if vals and weights.get(a, 0) > 0:
            areas[a] = half(statistics.median(vals))

    def headline(group: str) -> float | None:
        num = sum(weights[a] * areas[a] for a in rub["headlines"][group] if a in areas)
        den = sum(weights[a] for a in rub["headlines"][group] if a in areas)
        return round(num / den, 1) if den else None

    eff, craft = headline("effectiveness"), headline("craft")
    caps = []
    for cap in rub.get("caps") or []:
        w = cap["when"]
        hit = False
        if "criterion" in w:
            e = entries.get(w["criterion"]) or {}
            hit = "score" in e and e["score"] <= w["at_most"]
        elif "area_any_criterion" in w:
            hit = any("score" in e and e["score"] <= w["at_most"] for cid, e in entries.items() if crit[cid]["area"] == w["area_any_criterion"])
        if hit:
            caps.append(cap["reason"])
            if eff is not None and "effectiveness_max" in cap:
                eff = min(eff, float(cap["effectiveness_max"]))
            if cap.get("flag"):
                flags.append(cap["flag"])

    def val(key: str):
        if key in areas:
            return areas[key]
        e = entries.get(key) or {}
        return e.get("score")

    story_on = "STORY-1" in entries and "score" in entries["STORY-1"]
    rule = rub["works"]["with_story" if story_on else "without_story"]
    vals_ok = [val(k) for k in rule["works_if"]]
    vals_no = [(val(k), t) for k, t in rule["fails_if"].items()]
    if any(v is not None and v <= t for v, t in vals_no):
        works = "no"
    elif all(v is not None and v >= t for v, t in zip(vals_ok, rule["works_if"].values(), strict=True)):
        works = "yes"
    else:
        works = "partly"
    unstable = sorted(cid for cid, e in entries.items() if e.get("spread", 0) >= 2)
    return {"areas": areas, "headline": {"effectiveness": eff, "craft": craft, "works": works, "caps": caps, "flags": sorted(set(flags))},
            "unstable": unstable, "weights_used": weights, "outcome_used": outcome}


def record(run: Path, trace: Path | None = None) -> dict:
    """Score every output of a run whose reads are all in; write the scorecards, the history lines, the backlog and the report page."""
    st = status(run)
    if not st["ready"]:
        raise EvalError(f"{len(st['pending'])} read(s) still missing: " + ", ".join(p["read"] for p in st["pending"][:8]), "MAP-EVAL-005")
    ev = load_run(run)
    rub = load_rubric(run / "rubric.yaml")
    crit = criteria(rub)
    comps = set(rub["components"])
    cards, fs = [], []
    hist = evaluations_root(create=True) / "history.jsonl"
    for oid in ev["outputs"]:
        od = run / oid
        pk = json.loads((od / "packet.json").read_text(encoding="utf-8"))
        if pk.get("status") == "skipped":
            continue
        cold = validate_cold(read_json_file(od / "reads" / "cold.json"))
        reads = []
        for n in range(1, int(pk.get("passes") or 1) + 1):
            try:
                r, f2 = validate_graded(read_json_file(od / "reads" / f"graded-{n}.json"), pk["applicability"]["scored"], crit, comps, len(pk["frames"]), oid, n)
            except Exception as ex:  # noqa: BLE001
                raise EvalError(f"{oid} graded read {n} cannot be used ({ex}); ask for it again", "MAP-EVAL-005") from ex
            reads.append(r)
            fs += f2
        entries = combine(reads, pk["applicability"]["scored"])
        sc = score_card(rub, pk, cold, entries)
        for cid in sc["unstable"]:
            fs.append(finding("MAP-EVAL-006", "INFO", f"{oid}: {cid} scores spread by {entries[cid]['spread']} across {len(reads)} reads (unstable)"))
        if sc["headline"]["caps"]:
            fs.append(finding("MAP-EVAL-008", "INFO", f"{oid}: Effectiveness limited: {'; '.join(sc['headline']['caps'])}"))
        meas = json.loads((od / "measures.json").read_text(encoding="utf-8"))
        card = {"schema": SCHEMA, "rubric_version": ev["rubric_version"], "measure_version": MEASURE_VERSION, "plugin_version": plugin_version(),
                "model": ev.get("model"), "evaluated_at": now(), "run": run.name, "mode": ev.get("mode", "day"),
                "output": {"id": oid, "files": pk["files"], "sha256": pk["sha256"], "kind": pk["kind"], "medium": pk["intent"]["medium"], "frames": len(pk["frames"])},
                "intent": pk["intent"], "cold_read": cold, "applicability": {"weights": sc["weights_used"], "outcome": sc["outcome_used"],
                                                                             "na": pk["applicability"]["na"] + [{"id": c, "reason": e["reason"], "rule": "grader"} for c, e in entries.items() if e.get("na")]},
                "measures": _strip_measures(meas), "scores": [{"id": c, **e} for c, e in entries.items()], "areas": sc["areas"], "headline": sc["headline"],
                "passes": len(reads), "unstable": sc["unstable"]}
        (run / f"{oid}.eval.json").write_text(json.dumps(card, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        cards.append(card)
        line = {"date": card["evaluated_at"][:10], "at": card["evaluated_at"], "run": run.name, "output": oid, "files": pk["files"], "kind": pk["kind"],
                "medium": pk["intent"]["medium"], "family": pk["intent"]["family"], "outcome": sc["outcome_used"], "effectiveness": sc["headline"]["effectiveness"],
                "craft": sc["headline"]["craft"], "works": sc["headline"]["works"], "areas": sc["areas"], "scores": {c: e["score"] for c, e in entries.items() if "score" in e},
                "na_by_grader": [c for c, e in entries.items() if e.get("na")], "unstable": sc["unstable"], "rubric_version": ev["rubric_version"],
                "measure_version": MEASURE_VERSION, "plugin_version": card["plugin_version"], "mode": card["mode"], "passes": len(reads),
                "candidates": [{"criterion": c, "component": k["component"]} for c, e in entries.items() for k in e.get("candidates") or []]}
        with open(hist, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    backlog = build_backlog(cards, rub)
    (run / "backlog.json").write_text(json.dumps(backlog, indent=1, ensure_ascii=False), encoding="utf-8")
    page = report_html(cards, rub, backlog, run)
    (run / "report.html").write_text(page, encoding="utf-8")
    (run / "findings.json").write_text(json.dumps(fs, indent=1, ensure_ascii=False), encoding="utf-8")
    ev["recorded_at"] = now()
    (run / "evaluation.json").write_text(json.dumps(ev, indent=1), encoding="utf-8")
    res = {"ok": True, "run": str(run), "report": str(run / "report.html"), "scorecards": [str(run / f"{c['output']['id']}.eval.json") for c in cards],
           "summary": [{"output": c["output"]["id"], "effectiveness": c["headline"]["effectiveness"], "craft": c["headline"]["craft"], "works": c["headline"]["works"]} for c in cards],
           "findings": fs, "backlog": backlog[:5]}
    if trace:
        res["trace"] = attach_to_trace(trace, cards, backlog, run)
    return res


def _strip_measures(m: dict) -> dict:
    """The scorecard keeps the numbers, never pixels or paths."""
    return json.loads(json.dumps(m, default=str))


def build_backlog(cards: list[dict], rub: dict) -> list[dict]:
    """Improvement candidates grouped by component, most frequent and lowest-scoring first. They reach TODO.md only when the owner accepts them."""
    groups: dict[str, dict] = {}
    for c in cards:
        for s in c["scores"]:
            for k in s.get("candidates") or []:
                g = groups.setdefault(k["component"], {"component": k["component"], "about": rub["components"].get(k["component"], "not assigned to a component"),
                                                      "count": 0, "outputs": set(), "criteria": set(), "lowest": 5, "examples": []})
                g["count"] += 1
                g["outputs"].add(c["output"]["id"])
                g["criteria"].add(s["id"])
                g["lowest"] = min(g["lowest"], s.get("score", 5))
                if len(g["examples"]) < 4:
                    g["examples"].append({"output": c["output"]["id"], "criterion": s["id"], "score": s.get("score"), "text": k["text"]})
    out = []
    for g in groups.values():
        out.append({**g, "outputs": sorted(g["outputs"]), "criteria": sorted(g["criteria"])})
    out.sort(key=lambda g: (-g["count"], g["lowest"], g["component"]))
    return out


# ---------------------------------------------------------------------------------------------------------------- pages
def _page(title: str, parts: list[str], gen_note: str, doc_type: str) -> str:
    import trace_page as tp
    b = tp.brand()
    style = (f"{tp.font_faces()}\n:root{{{tp.css_vars(b['print'])}color-scheme:light}}\n"
             f":root[data-theme=dark]{{{tp.css_vars(b['screen'])}color-scheme:dark}}\n{tp.CSS}\n{EXTRA_CSS}")
    toggle = ('<div class="toggle" role="group" aria-label="Theme"><button data-v="light" aria-pressed="false">Light</button>'
              '<button data-v="dark" aria-pressed="false">Dark</button><button data-v="auto" aria-pressed="true">Auto</button></div>')
    foot = (f'<footer><div>{esc(gen_note)}</div>' + (f'<div class="mark">{tp.logo_svg(b["logo"], 18, b["name"])}<span>{esc(b["name"])}</span></div>' if b["name"] else "") + "</footer>")
    return ("<!doctype html>\n<html lang=\"en\" data-theme=\"light\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{esc(title)}</title><style>{style}</style></head><body data-doc-type=\"{doc_type}\">{toggle}{''.join(parts)}{foot}<script>{tp.JS}</script></body></html>\n")


EXTRA_CSS = """
.ev-grid{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,1fr);gap:28px;align-items:start}
.ev-grid img{max-width:100%;height:auto;border:1px solid var(--line2);border-radius:6px;display:block}
.nums{display:flex;gap:14px;flex-wrap:wrap;margin:6px 0 12px}
.nums .tile{min-width:120px}
.bar{display:grid;grid-template-columns:150px 1fr 42px;gap:10px;align-items:center;font-size:13.5px;margin:5px 0}
.bar .t{height:12px;background:var(--elevated);border:1px solid var(--line);border-radius:3px;overflow:hidden}
.bar .t i{display:block;height:100%;background:var(--a1)}
.quote{border-left:3px solid var(--a2);padding:4px 12px;margin:10px 0;color:var(--text2)}
.small{font-size:13px;color:var(--text2)}
@media (max-width:820px){.ev-grid{grid-template-columns:1fr}.bar{grid-template-columns:110px 1fr 36px}}
"""


def _thumb_data_uri(p: Path, width: int = 900) -> str:
    import base64
    Image = _pil()
    with Image.open(p) as im:
        im = im.convert("RGB")
        if im.size[0] > width:
            im = im.resize((width, max(1, round(im.size[1] * width / im.size[0]))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _pill(works: str) -> str:
    cls = {"yes": "ok", "partly": "warn", "no": "bad"}.get(works, "info")
    return f'<span class="pill {cls}">works: {esc(works)}</span>'


def _bars(areas: dict, rub: dict) -> str:
    rows = []
    for a in rub["areas"]:
        if a in areas:
            rows.append(f'<div class="bar"><span>{esc(rub["areas"][a]["name"])}</span><span class="t"><i style="width:{20 * areas[a]:.0f}%"></i></span><b class="n">{areas[a]:g}</b></div>')
    return "".join(rows)


def report_html(cards: list[dict], rub: dict, backlog: list[dict], run: Path) -> str:
    import trace_page as tp
    eff = [c["headline"]["effectiveness"] for c in cards if c["headline"]["effectiveness"] is not None]
    craft = [c["headline"]["craft"] for c in cards if c["headline"]["craft"] is not None]
    works = [c["headline"]["works"] for c in cards]
    b = tp.brand()
    tiles = [(len(cards), "Outputs"), (f"{statistics.fmean(eff):.1f}" if eff else "-", "Effectiveness (mean)"), (f"{statistics.fmean(craft):.1f}" if craft else "-", "Craft (mean)"),
             (f"{works.count('yes')} / {works.count('partly')} / {works.count('no')}", "Works: yes / partly / no"), (cards[0]["rubric_version"] if cards else "-", "Rubric version"),
             (cards[0]["passes"] if cards else "-", "Graded reads per output")]
    parts = [f'<section class="hero"><div class="wrap"><div><div>{tp.logo_svg(b["logo"], 54, b["name"])}</div><p class="eyebrow">Map evaluation (observed)</p>'
             '<h1>How good the finished maps are, and why</h1><p class="sub">Graded from the pictures alone, against a rubric that is meant to change. '
             'These scores are a record for improving the plugin; they never change a map or a build.</p></div><div class="tiles">'
             + "".join(f'<div class="tile"><b>{esc(v)}</b><span>{esc(k)}</span></div>' for v, k in tiles) + "</div></div></section>"]
    crit = criteria(rub)
    for c in cards:
        oid = c["output"]["id"]
        first = run / oid / "cold" / "frame-01.png"
        img = f'<img alt="{esc(oid)}" src="{_thumb_data_uri(first)}">' if first.is_file() else ""
        h = c["headline"]
        nums = (f'<div class="nums"><div class="tile"><b>{esc(h["effectiveness"] if h["effectiveness"] is not None else "-")}</b><span>Effectiveness</span></div>'
                f'<div class="tile"><b>{esc(h["craft"] if h["craft"] is not None else "-")}</b><span>Craft</span></div></div>')
        intent = c["intent"]
        meta = (f'<p class="small">{esc(c["output"]["kind"])} · {esc(intent["medium"])} · {esc(intent["family"])} · outcome {esc(c["applicability"]["outcome"] or "unknown")}'
                f'{" (inferred)" if "outcome_inferred" in h["flags"] else (" (from the cold read)" if "outcome_from_cold_read" in h["flags"] else "")} · style {esc(intent["style_pack"])} · '
                f'{c["output"]["frames"]} frame(s)</p>')
        quote = (f'<p class="label">Cold read</p><div class="quote">“{esc(c["cold_read"]["takeaway"])}”</div>'
                 f'<p class="small">Intended: {esc(intent.get("message") or "(no message)")}</p>')
        capline = ("<p class='small'>Limited: " + esc("; ".join(h["caps"])) + "</p>") if h["caps"] else ""
        flags = ("<p class='small'>Flags: " + esc(", ".join(h["flags"] + [f"unstable {u}" for u in c["unstable"]])) + "</p>") if (h["flags"] or c["unstable"]) else ""
        rows = []
        for s in c["scores"]:
            name = crit.get(s["id"], {}).get("name", "")
            if "score" in s:
                ev_ = s["evidence"]
                cand = "; ".join(f"{k['component']}: {k['text']}" for k in s.get("candidates") or [])
                rows.append([f"<b>{esc(s['id'])}</b> {esc(name)}", f"{s['score']:g}" + (f" <span class='small'>({', '.join(str(x) for x in s['scores'])})</span>" if len(s.get('scores') or []) > 1 else ""),
                             esc(s["confidence"]), f"<span class='small'>frame {ev_['frame']} {esc(ev_['box'])}</span> {esc(ev_['seen'])}", esc(cand)])
            elif s.get("na"):
                rows.append([f"<b>{esc(s['id'])}</b> {esc(name)}", "n/a", "-", f"<span class='small'>grader: {esc(s['reason'])}</span>", ""])
            else:
                rows.append([f"<b>{esc(s['id'])}</b> {esc(name)}", "-", "-", f"<span class='small'>unscored: {esc(s.get('unscored'))}</span>", ""])
        na = [x for x in c["applicability"]["na"] if x.get("rule") != "grader" and not str(x["reason"]).startswith("area ")]
        na_line = ("<p class='small'>Not scored: " + esc("; ".join(f"{x['id']} ({x['reason']})" for x in na)) + "</p>") if na else ""
        body = (f'<div class="ev-grid"><div>{img}</div><div>{_pill(h["works"])}{nums}{meta}{quote}{_bars(c["areas"], rub)}{capline}{flags}</div></div>'
                + tp.table(["Criterion", "Score", "Confidence", "Evidence", "Improvement candidate"], rows, {1}) + na_line)
        parts.append(tp.section(oid, ", ".join(c["output"]["files"])[:120], body))
    if backlog:
        rows = [[f"<b>{esc(g['component'])}</b><br><span class='small'>{esc(g['about'])}</span>", str(g["count"]), esc(", ".join(g["criteria"])), str(g["lowest"]),
                 "<br>".join(f"<span class='small'>{esc(e['output'])} {esc(e['criterion'])} ({e['score']}):</span> {esc(e['text'])}" for e in g["examples"])] for g in backlog]
        parts.append(tp.section("Improvement backlog", "Where the plugin's functions fell short",
                                "<p>Grouped by the part of the plugin a low score most likely comes from. Nothing here is filed automatically: a candidate reaches TODO.md only when the owner accepts it.</p>"
                                + tp.table(["Component", "Candidates", "Criteria", "Lowest", "Examples"], rows, {1, 3})))
    notes = [f"Rubric {esc(cards[0]['rubric_version'] if cards else rub['rubric_version'])} ({esc(rub.get('status', ''))}); measurements version {MEASURE_VERSION}. Scores within one rubric major version compare; across versions they do not.",
             "Pass 0 fixed which criteria apply from the map's outcome, family and medium; pass 1 was a cold read by a fresh agent that saw only the pictures; pass 2 graded each criterion "
             "with evidence, from the pictures, the intent card, the measurements and the cold read. A score without evidence is void.",
             "Effectiveness (story, information, hierarchy, readability, honesty, orientation) and Craft (layout, style, medium) are kept apart, with the works verdict beside them, so a plain map that works is never hidden behind a low blended score.",
             "Observe only: no creation step reads these scores, the rubric or this page (release-gate test). Scorecards stay in the working folder; no picture goes into the history."]
    parts.append(tp.section("How this was measured", "Method", '<ul class="notes">' + "".join(f"<li>{n}</li>" for n in notes) + "</ul>"))
    return _page(f"Map evaluation {run.name}", parts, f"Map evaluation · {plugin_version() or ''} · {now()[:16].replace('T', ' ')} UTC", "map-evaluation-report")


def attach_to_trace(trace_json: Path, cards: list[dict], backlog: list[dict], run: Path) -> dict:
    """Add the Map quality (observed) section to a map-trace report: its JSON, then its HTML and PDF page again."""
    o = json.loads(trace_json.read_text(encoding="utf-8"))
    o["map_quality"] = {"run": run.name, "report": str(run / "report.html"), "rubric_version": cards[0]["rubric_version"] if cards else None,
                        "outputs": [{"output": c["output"]["id"], "effectiveness": c["headline"]["effectiveness"], "craft": c["headline"]["craft"],
                                     "works": c["headline"]["works"], "flags": c["headline"]["flags"]} for c in cards],
                        "top_components": [{"component": g["component"], "count": g["count"], "example": g["examples"][0]["text"] if g["examples"] else ""} for g in backlog[:3]]}
    trace_json.write_text(json.dumps(o, indent=1, default=str), encoding="utf-8")
    import map_trace
    stem = trace_json.with_suffix("")
    html_line, pdf_line = map_trace.write_page(o, stem, pdf=stem.with_suffix(".pdf").is_file())
    return {"html": html_line, "pdf": pdf_line}


# ---------------------------------------------------------------------------------------------------------------- 3.4.0 (F-5): scorecards read back
def _card_roots(outputs: list[dict], extra: list[str] | None = None) -> list[Path]:
    roots = [evaluations_root()]
    for o in outputs:
        for f in o.get("files") or [o.get("path")]:
            if not f:
                continue
            for up in list(Path(f).parents)[:4]:
                roots.append(up / gc.WORKDIR_NAME / EVAL_DIRNAME)
    roots += [Path(x) / gc.WORKDIR_NAME / EVAL_DIRNAME for x in extra or []]
    seen, keep = set(), []
    for r in roots:
        k = str(r)
        if k not in seen and r.is_dir():
            seen.add(k)
            keep.append(r)
    return keep


def recorded_cards(outputs: list[dict], extra: list[str] | None = None) -> dict[str, dict]:
    """The newest scorecard already recorded for each output (same file name and SHA-256 as the file now), by the output's id. Nothing is read
    again and nothing is graded: a trace of a session whose maps were evaluated before fills its Map quality section from these."""
    want: dict[str, dict[str, str]] = {}
    for o in outputs:
        hashes = {}
        for f in o.get("files") or [o.get("path")]:
            try:
                hashes[Path(f).name] = _sha(Path(f))
            except (OSError, TypeError):
                pass
        if hashes:
            want[o["id"]] = hashes
    best: dict[str, dict] = {}
    for root in _card_roots(outputs, extra):
        for cf in root.glob("*/*.eval.json"):
            try:
                card = json.loads(cf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            got = (card.get("output") or {}).get("sha256") or {}
            for oid, hashes in want.items():
                if any(got.get(n) == h for n, h in hashes.items()):
                    if oid not in best or str(card.get("evaluated_at")) > str(best[oid].get("evaluated_at")):
                        best[oid] = {**card, "_file": str(cf)}
    return best


def evaluate_mode() -> str:
    """3.4.0 (F-5), 4.2.0: `auto` (the default since 4.2.0), `ask` or `off`, from GMAP_EVALUATE or `evaluate:` in ~/.geo-mapping/config.yaml (GEO_MAPPING_HOME in tests).
    `auto` lets the map-trace skill run the cold and graded reads without asking; they stay fresh agents and observe only."""
    v = os.environ.get("GMAP_EVALUATE")
    if not v:
        home = os.environ.get("GEO_MAPPING_HOME")
        cp = (Path(home) if home else Path.home() / gc.WORKDIR_NAME) / "config.yaml"
        try:
            import yaml
            v = (yaml.safe_load(cp.read_text(encoding="utf-8")) or {}).get("evaluate") if cp.is_file() else None
        except Exception:  # noqa: BLE001
            v = None
    v = str(v or "").strip().lower()
    return v if v in ("ask", "off") else "auto"          # 4.2.0 (decision D12): graded without asking unless the person set ask or off


# ---------------------------------------------------------------------------------------------------------------- 4.2.0: records that last
HANDBACK_KIND = "geo-spatial-mapping evaluation hand-back"


def history_key(r: dict) -> tuple:
    return (str(r.get("output")), str(r.get("at") or r.get("date")), str(r.get("run")))


def export_history(out: Path, run: str | None = None, since: str | None = None) -> dict:
    """4.2.0: the history lines of a run (or since a date) as one small file a cloud session hands back, so its scores outlive it. No picture
    and no scorecard evidence is in it: the lines the history already keeps, and nothing else."""
    rows = history()
    if run:
        rows = [r for r in rows if r.get("run") == Path(run).name]
    if since:
        rows = [r for r in rows if str(r.get("date") or "") >= since]
    doc = {"kind": HANDBACK_KIND, "version": 1, "plugin_version": plugin_version(), "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "lines": rows}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    return {"file": str(out), "lines": len(rows)}


def import_history(src: Path) -> dict:
    """4.2.0: add a hand-back file's lines to this working folder's history (the person's computer), each line once."""
    try:
        doc = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        raise EvalError(f"{src} cannot be read as a hand-back file ({type(ex).__name__})", "MAP-EVAL-005") from None
    if not isinstance(doc, dict) or doc.get("kind") != HANDBACK_KIND or not isinstance(doc.get("lines"), list):
        raise EvalError(f"{src} is not an evaluation hand-back file (map_evaluate.py export writes one)", "MAP-EVAL-005")
    have = {history_key(r) for r in history()}
    hist = evaluations_root(create=True) / "history.jsonl"
    added = 0
    with open(hist, "a", encoding="utf-8") as fh:
        for r in doc["lines"]:
            if not isinstance(r, dict) or not r.get("output") or history_key(r) in have:
                continue
            have.add(history_key(r))
            fh.write(json.dumps({**r, "imported_from": src.name}, ensure_ascii=False) + "\n")
            added += 1
    return {"history": str(hist), "added": added, "already_there": len(doc["lines"]) - added}


def tracker_lines(run: str | None = None, rows: list[dict] | None = None) -> list[dict]:
    """4.2.0: what a tracker entry carries for each scored output: the two numbers, the verdict and the rubric's version, as one line of text."""
    rows = rows if rows is not None else history()
    if run:
        rows = [r for r in rows if r.get("run") == Path(run).name]
    latest: dict[str, dict] = {}
    for r in rows:
        if r.get("output") and (r["output"] not in latest or str(r.get("at")) > str(latest[r["output"]].get("at"))):
            latest[r["output"]] = r
    out = []
    for oid, r in sorted(latest.items()):
        out.append({"output": oid, "effectiveness": r.get("effectiveness"), "craft": r.get("craft"), "works": r.get("works"), "outcome": r.get("outcome"),
                    "rubric_version": r.get("rubric_version"), "date": r.get("date"),
                    "text": f"picture rubric {r.get('rubric_version')}: Effectiveness {r.get('effectiveness')}, Craft {r.get('craft')}, works: {r.get('works')}"
                            + (f" (outcome {r.get('outcome')})" if r.get("outcome") else "")})
    return out


def sync_owner(anchor_set: Path, owner: Path) -> dict:
    """4.2.0: bring the owner's score sheet in step with the anchor set: a line for every anchor, scored lines kept as they are, lines of
    anchors no longer in the set kept at the end under `retired`. The sheet had 22 lines for 30 anchors."""
    import yaml
    aset = gc.load_yaml(anchor_set) or {}
    cur = gc.load_yaml(owner) or {}
    old = cur.get("scores") or {}
    ids = [a["id"] for a in aset.get("anchors") or [] if a.get("id")]
    blank = {"effectiveness": None, "craft": None, "works": None, "kind": None, "areas": {}}
    scores = {i: (old.get(i) or dict(blank)) for i in ids}
    retired = {k: v for k, v in old.items() if k not in scores and any(v.get(x) is not None for x in ("effectiveness", "craft", "works"))}
    head = "".join(ln + "\n" for ln in owner.read_text(encoding="utf-8").splitlines() if ln.startswith("#")) if owner.is_file() else ""
    doc = {"rubric_version": cur.get("rubric_version") or aset.get("rubric_version_first_scored") or "0.1", "scored_by": cur.get("scored_by") or "owner",
           "scored_on": cur.get("scored_on"), "scores": scores, **({"retired": retired} if retired else {})}
    owner.write_text(head + "\n" + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=None, width=200), encoding="utf-8")
    return {"anchors": len(ids), "lines": len(scores), "added": sorted(set(ids) - set(old)), "scored": sum(1 for v in scores.values() if v.get("effectiveness") is not None)}


# ---------------------------------------------------------------------------------------------------------------- 4.2.0: the owner's scoring page
SCORING_THUMB_PX = 1100
SCORING_CSS = """
.anchor{border-top:1px solid var(--line);padding:22px 0;display:grid;grid-template-columns:minmax(0,1.5fr) minmax(260px,1fr);gap:22px}
.anchor img{width:100%;height:auto;border:1px solid var(--line);background:var(--surface)}
.anchor .nofile{border:1px dashed var(--line);padding:18px;color:var(--text2)}
.anchor h3{margin:0 0 4px;font-size:16px}.anchor .pair{color:var(--text2);font-size:12px;margin-bottom:12px}
.field{margin:10px 0}.field label{display:flex;justify-content:space-between;font-size:13px;font-weight:600}
.field input[type=range]{width:100%}.field .val{font-variant-numeric:tabular-nums}
.seg{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px}.seg button{border:1px solid var(--line);background:transparent;color:inherit;padding:5px 10px;border-radius:6px;cursor:pointer;font:inherit;font-size:13px}
.seg button[aria-pressed=true]{background:var(--a1);color:var(--bg);border-color:var(--a1)}
.sbar{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 0;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.sbar button{border:1px solid var(--a1);background:var(--a1);color:var(--bg);padding:7px 14px;border-radius:6px;cursor:pointer;font:inherit}
.sbar button.ghost{background:transparent;color:inherit;border-color:var(--line)}
.scale{font-size:12px;color:var(--text2);margin:6px 0 0}
@media (max-width:820px){.anchor{grid-template-columns:1fr}}
"""
SCORING_JS = r"""
(function(){
  var KEY='gm-anchor-scores:'+document.body.dataset.set, st={};
  try{st=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){st={}}
  var S=JSON.parse(document.getElementById('seed').textContent||'{}'), seed=S.seed||{};
  Object.keys(seed).forEach(function(k){if(!st[k])st[k]=seed[k]});
  function save(){try{localStorage.setItem(KEY,JSON.stringify(st))}catch(e){};count()}
  function count(){var n=0,t=0;document.querySelectorAll('.anchor').forEach(function(a){t++;var s=st[a.dataset.id]||{};if(s.effectiveness!=null&&s.craft!=null&&s.works)n++});
    document.getElementById('count').textContent=n+' of '+t+' scored'}
  document.querySelectorAll('.anchor').forEach(function(a){
    var id=a.dataset.id; st[id]=st[id]||{};
    a.querySelectorAll('input[type=range]').forEach(function(r){
      var f=r.dataset.f, out=a.querySelector('.val[data-f="'+f+'"]');
      function show(){out.textContent=st[id][f]==null?'not scored':Number(st[id][f]).toFixed(1)}
      if(st[id][f]!=null)r.value=st[id][f]; show();
      r.addEventListener('input',function(){st[id][f]=parseFloat(r.value);show();save()});
    });
    a.querySelectorAll('.seg').forEach(function(g){
      var f=g.dataset.f;
      function show(){g.querySelectorAll('button').forEach(function(b){b.setAttribute('aria-pressed',String(st[id][f]===b.dataset.v))})}
      show();
      g.querySelectorAll('button').forEach(function(b){b.addEventListener('click',function(){st[id][f]=(st[id][f]===b.dataset.v?null:b.dataset.v);show();save()})});
    });
  });
  function yaml(){
    var d=new Date().toISOString().slice(0,10), L=[];
    String(S.head||'').split('\n').forEach(function(x){if(x)L.push(x)});
    L.push('','rubric_version: "'+document.body.dataset.rubric+'"','scored_by: owner','scored_on: '+d,'','scores:');
    document.querySelectorAll('.anchor').forEach(function(a){var s=st[a.dataset.id]||{};
      function v(x){return x==null||x===''?'null':x}
      L.push('  '+a.dataset.id+': {effectiveness: '+v(s.effectiveness)+', craft: '+v(s.craft)+', works: '+(s.works?'"'+s.works+'"':'null')+', kind: '+v(s.kind)+', areas: {}}')});
    return L.join('\n')+'\n';
  }
  document.getElementById('save').addEventListener('click',function(){
    var b=new Blob([yaml()],{type:'text/yaml'}),u=URL.createObjectURL(b),a=document.createElement('a');a.href=u;a.download='owner-scores.yaml';document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(u)});
  document.getElementById('copy').addEventListener('click',function(){if(navigator.clipboard)navigator.clipboard.writeText(yaml())});
  count();
})();
"""


def _thumb_uri(path: Path) -> str | None:
    """A still as a JPEG data URI at most SCORING_THUMB_PX wide; None for anything that is not a picture Pillow reads."""
    import base64
    try:
        from PIL import Image
        im = Image.open(path)
        im.load()
    except Exception:  # noqa: BLE001
        return None
    if im.mode not in ("RGB", "L"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
        im = bg
    if im.width > SCORING_THUMB_PX:
        im = im.resize((SCORING_THUMB_PX, max(1, round(im.height * SCORING_THUMB_PX / im.width))), Image.LANCZOS)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=82, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def scoring_page(anchor_set: Path, owner: Path | None, out: Path, root: Path | None = None) -> dict:
    """4.2.0 (decision D14): one page to score the anchor set by eye, in place of editing YAML: each anchor's picture, two sliders
    (Effectiveness, Craft), the works verdict and the kind. Nothing of the evaluator's is on it, so the scores are the owner's own. The
    page keeps what was entered in the browser, and its Save button downloads `owner-scores.yaml`, to put beside the anchor set."""
    aset = gc.load_yaml(anchor_set) or {}
    own = ((gc.load_yaml(owner) or {}).get("scores") or {}) if owner and owner.is_file() else {}
    base = Path(root) if root else (anchor_set.parent / str(aset.get("root") or "."))
    rub = load_rubric()
    parts = [f'<header class="hero"><div class="wrap"><h1>Score the anchor set</h1><p class="lede">{len(aset.get("anchors") or [])} outputs. Score each by eye, before looking at '
             "any score the map evaluation gave it: Effectiveness (does it do its job for the reader), Craft (is it well made), whether it works, and its kind. "
             "Mark at least two <b>rough-but-works</b> and two <b>polished-but-empty</b>.</p>"
             f'<p class="scale">5 {esc(rub["scale"][5])}<br>3 {esc(rub["scale"][3])}<br>1 {esc(rub["scale"][1])}</p></div></header><main class="wrap">',
             '<div class="sbar"><button id="save">Save owner-scores.yaml</button><button id="copy" class="ghost">Copy as text</button><span id="count"></span>'
             '<span class="scale">Scores are kept in this browser as you go. Put the saved file in the anchors folder, over the old one.</span></div>']
    n_pic = 0
    for a in aset.get("anchors") or []:
        aid = str(a.get("id"))
        f = (base / str(a["file"])) if a.get("file") else None
        uri = _thumb_uri(f) if f is not None and f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") else None
        if uri:
            n_pic += 1
            pic = f'<img alt="{esc(aid)}" loading="lazy" src="{uri}">'
        elif f is not None:
            pic = (f'<div class="nofile">{esc(f.suffix.lstrip(".").upper() or "File")}: open <code>{esc(a["file"])}</code> from the challenge folder to look at it'
                   + ("" if f.is_file() else " (not found here)") + "</div>")
        else:
            pic = '<div class="nofile">No file for this day yet. Leave it unscored, or add the file to the anchor set.</div>'

        def slider(fld, label):
            return (f'<div class="field"><label>{label} <span class="val" data-f="{fld}"></span></label>'
                    f'<input type="range" min="1" max="5" step="0.5" value="3" data-f="{fld}" aria-label="{label} for {esc(aid)}"></div>')

        def seg(fld, label, opts):
            return (f'<div class="field"><label>{label}</label><div class="seg" data-f="{fld}">'
                    + "".join(f'<button type="button" data-v="{v}">{esc(t)}</button>' for v, t in opts) + "</div></div>")
        parts.append(f'<section class="anchor" data-id="{esc(aid)}"><div>{pic}</div><div><h3>{esc(aid)}</h3><div class="pair">{esc(a.get("pair") or "")}</div>'
                     + slider("effectiveness", "Effectiveness") + slider("craft", "Craft")
                     + seg("works", "Works?", [("yes", "Yes"), ("partly", "Partly"), ("no", "No")])
                     + seg("kind", "Kind", [("ordinary", "Ordinary"), ("rough-but-works", "Rough but works"), ("polished-but-empty", "Polished but empty")])
                     + "</div></section>")
    seed = {k: {x: (({True: "yes", False: "no"}.get(v[x], v[x])) if x == "works" else v[x]) for x in ("effectiveness", "craft", "works", "kind") if v.get(x) is not None}
            for k, v in own.items() if isinstance(v, dict)}
    head = "\n".join(ln for ln in (owner.read_text(encoding="utf-8").splitlines() if owner and owner.is_file() else []) if ln.startswith("#"))
    parts.append("</main>")
    parts.append('<script type="application/json" id="seed">' + json.dumps({"seed": seed, "head": head}).replace("</", "<\\/") + "</script>")
    html = _page("Score the anchor set", parts, f"Anchor set {aset.get('anchor_set') or anchor_set.stem}, rubric {rub['rubric_version']}. The owner's own scores; "
                 "no evaluator score is shown here.", "anchor-scoring")
    html = html.replace("</style>", SCORING_CSS + "</style>", 1).replace("<body ", f'<body data-set="{esc(aset.get("anchor_set") or anchor_set.stem)}" data-rubric="{esc(rub["rubric_version"])}" ', 1)
    html = html.replace("</body>", f"<script>{SCORING_JS}</script></body>", 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return {"page": str(out), "anchors": len(aset.get("anchors") or []), "pictures": n_pic, "bytes": len(html.encode("utf-8"))}


# ---------------------------------------------------------------------------------------------------------------- history, trend, anchors, review
def history(path: Path | None = None) -> list[dict]:
    p = path or evaluations_root() / "history.jsonl"
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _line_svg(series: dict[str, list[tuple[str, float]]], w: int = 900, h: int = 260) -> str:
    pts = [v for s in series.values() for _, v in s]
    days = sorted({d for s in series.values() for d, _ in s})
    if not pts or not days:
        return ""
    x = {d: 50 + (w - 70) * (i / max(1, len(days) - 1)) for i, d in enumerate(days)}
    y = lambda v: h - 30 - (h - 50) * (v - 1) / 4  # noqa: E731
    cols = ["var(--a1)", "var(--a2)", "var(--a3)", "var(--a4)"]
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Scores over time">']
    for v in (1, 2, 3, 4, 5):
        out.append(f'<line x1="50" x2="{w - 20}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="var(--line)"/><text x="40" y="{y(v) + 4:.1f}" font-size="11" text-anchor="end" fill="var(--text2)">{v}</text>')
    for i, (name, s) in enumerate(series.items()):
        c = cols[i % len(cols)]
        path = " ".join(f"{'M' if j == 0 else 'L'}{x[d]:.1f},{y(v):.1f}" for j, (d, v) in enumerate(s))
        out.append(f'<path d="{path}" fill="none" stroke="{c}" stroke-width="2"/>' + "".join(f'<circle cx="{x[d]:.1f}" cy="{y(v):.1f}" r="3" fill="{c}"/>' for d, v in s))
        out.append(f'<text x="{60 + 140 * i}" y="16" font-size="12" fill="{c}">● {esc(name)}</text>')
    for d in (days[0], days[-1]):
        out.append(f'<text x="{x[d]:.1f}" y="{h - 8}" font-size="11" text-anchor="middle" fill="var(--text2)">{esc(d)}</text>')
    return "".join(out) + "</svg>"


def trend_html(rows: list[dict], rub: dict) -> str:
    import trace_page as tp
    major = rubric_major(rub["rubric_version"])
    rows = [r for r in rows if rubric_major(r.get("rubric_version", "0")) == major and r.get("mode", "day") == "day"]
    by_day: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        d = by_day.setdefault(r["date"], {"Effectiveness": [], "Craft": []})
        if r.get("effectiveness") is not None:
            d["Effectiveness"].append(r["effectiveness"])
        if r.get("craft") is not None:
            d["Craft"].append(r["craft"])
    series = {k: [(d, round(statistics.fmean(v[k]), 2)) for d, v in sorted(by_day.items()) if v[k]] for k in ("Effectiveness", "Craft")}
    by_ver: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        bv = by_ver.setdefault(r.get("plugin_version") or "?", {})
        for a, v in (r.get("areas") or {}).items():
            bv.setdefault(a, []).append(v)
    vrows = [[esc(v)] + [f"{statistics.fmean(d[a]):.1f}" if a in d else "-" for a in rub["areas"]] for v, d in sorted(by_ver.items())]
    comp: dict[str, int] = {}
    for r in rows:
        for k in r.get("candidates") or []:
            comp[k["component"]] = comp.get(k["component"], 0) + 1
    crow = [[esc(k), str(v)] for k, v in sorted(comp.items(), key=lambda kv: -kv[1])[:12]]
    parts = [f'<section class="hero"><div class="wrap"><div><p class="eyebrow">Map evaluation trend</p><h1>Map quality over time</h1>'
             f'<p class="sub">{len(rows)} evaluated output(s), rubric {esc(major)}.x only; calibration runs left out.</p></div></div></section>',
             tp.section("By day", "Effectiveness and Craft", _line_svg(series) or "<p>No scores yet.</p>"),
             tp.section("By release", "Area scores by plugin version", tp.table(["Version"] + list(rub["areas"]), vrows, set(range(1, len(rub["areas"]) + 1))) if vrows else "<p>None yet.</p>"),
             tp.section("Backlog", "Components named most often", tp.table(["Component", "Candidates"], crow, {1}) if crow else "<p>None yet.</p>")]
    return _page("Map evaluation trend", parts, f"Map evaluation trend · {now()[:10]}", "map-evaluation-trend")


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return round(num / den, 3) if den else None


def _anchor_key(files: list[str]) -> set[str]:
    return {Path(f).stem.lower() for f in files}


def anchors_agreement(anchor_set: Path, owner: Path, rows: list[dict] | None = None) -> dict:
    """Agreement with the owner's scores (spec 9.3): Spearman on Effectiveness, the share of area scores within one point, and every
    disagreement of two points or more, to be reviewed."""
    aset = gc.load_yaml(anchor_set) or {}
    own = (gc.load_yaml(owner) or {}).get("scores") or {}
    rows = rows if rows is not None else history()
    latest: dict[str, dict] = {}
    for a in aset.get("anchors") or []:
        if not a.get("file"):
            continue
        stem = Path(str(a["file"]).replace("\\", "/")).stem.lower()
        for r in rows:
            if stem in _anchor_key(r.get("files") or []):
                if a["id"] not in latest or r["at"] > latest[a["id"]]["at"]:
                    latest[a["id"]] = r
    pairs, area_hits, area_total, dis = [], 0, 0, []
    for aid, r in latest.items():
        o = own.get(aid) or {}
        if o.get("effectiveness") is not None and r.get("effectiveness") is not None:
            pairs.append((float(o["effectiveness"]), float(r["effectiveness"])))
            if abs(float(o["effectiveness"]) - r["effectiveness"]) >= 2:
                dis.append({"anchor": aid, "what": "effectiveness", "owner": o["effectiveness"], "evaluator": r["effectiveness"]})
        if o.get("craft") is not None and r.get("craft") is not None and abs(float(o["craft"]) - r["craft"]) >= 2:
            dis.append({"anchor": aid, "what": "craft", "owner": o["craft"], "evaluator": r["craft"]})
        ow = {True: "yes", False: "no"}.get(o.get("works"), o.get("works"))      # plain YAML reads an unquoted yes or no as true or false
        if ow and ow != r.get("works"):
            dis.append({"anchor": aid, "what": "works", "owner": ow, "evaluator": r.get("works")})
        for a, v in (o.get("areas") or {}).items():
            if v is None or a not in (r.get("areas") or {}):
                continue
            area_total += 1
            d = abs(float(v) - r["areas"][a])
            area_hits += d <= 1
            if d >= 2:
                dis.append({"anchor": aid, "what": a, "owner": v, "evaluator": r["areas"][a]})
    rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
    within = round(area_hits / area_total, 3) if area_total else None
    return {"anchors": len(aset.get("anchors") or []), "evaluated": len(latest), "owner_scored": sum(1 for a in latest if own.get(a)),
            "spearman_effectiveness": rho, "areas_within_1": within, "targets": {"spearman": 0.8, "areas_within_1": 0.8},
            "meets_targets": bool(rho is not None and within is not None and rho >= 0.8 and within >= 0.8), "disagreements": dis,
            "note": "targets to learn from, not gates (spec 9.3); every disagreement of 2 or more changes a description or a weight (a new rubric version), or is recorded as taste"}


def review(days: int | None = None, owner: Path | None = None, anchor_set: Path | None = None) -> dict:
    """The 10-day rubric review (spec 9.3)."""
    rows = history()
    if days:
        cut = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        rows = [r for r in rows if r["date"] >= cut]
    by_c: dict[str, list[float]] = {}
    unstable: dict[str, int] = {}
    na: dict[str, int] = {}
    for r in rows:
        for c, v in (r.get("scores") or {}).items():
            by_c.setdefault(c, []).append(float(v))
        for c in r.get("unstable") or []:
            unstable[c] = unstable.get(c, 0) + 1
        for c in r.get("na_by_grader") or []:
            na[c] = na.get(c, 0) + 1
    flat = sorted(c for c, v in by_c.items() if len(v) >= 5 and statistics.pstdev(v) < 0.35)
    out = {"outputs": len(rows), "window_days": days, "criteria": {c: {"n": len(v), "mean": round(statistics.fmean(v), 2), "sd": round(statistics.pstdev(v), 2)} for c, v in sorted(by_c.items())},
           "unstable": unstable, "na_proposals": na, "flat_criteria": flat,
           "questions": ["Which unstable criteria need clearer score descriptions?", "Are the grader's n/a proposals a missing applicability rule?",
                         "Does a flat criterion tell anything, or should it be merged or dropped?", "Do the owner's disagreements change a weight, or are they taste?"]}
    if owner and anchor_set:
        out["anchors"] = anchors_agreement(anchor_set, owner, rows)
    return out


# ---------------------------------------------------------------------------------------------------------------- the pipeline's stages (called by pipelines.py)
def plan_outputs(files: list[str], outputs_list: str | None, anchor_set: str | None) -> tuple[list[dict], list[dict], dict]:
    """The outputs a map-evaluate plan covers: files given, a map-trace outputs list, or an anchor set. -> (outputs, findings, specs)."""
    specs: dict = {}
    paths = list(files or [])
    if outputs_list:
        lst = json.loads(Path(outputs_list).read_text(encoding="utf-8"))
        for it in lst.get("outputs") or []:
            paths.append(it["path"])
            if it.get("spec"):
                specs[str(it["path"])] = it["spec"]
    if anchor_set:
        aset = gc.load_yaml(Path(anchor_set)) or {}
        root = Path(str(aset.get("root") or "."))
        if not root.is_absolute():                                  # relative to the anchor file, never to where the command runs
            root = (Path(anchor_set).parent / root).resolve()
        for a in aset.get("anchors") or []:
            if a.get("file"):
                f = Path(str(a["file"]).replace("\\", "/"))
                paths.append(str(f if f.is_absolute() else root / f))
    outs, fs = discover(paths, specs)
    return outs, fs, specs


def start_run(run_id: str, outputs: list[dict], opts: dict) -> tuple[Path, dict, list[dict]]:
    rub = load_rubric()
    run = evaluations_root(create=True) / run_id
    run.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rubric_path(), run / "rubric.yaml")                  # the rubric the run was graded with, kept with it
    fs, packed = [], []
    for o in outputs:
        pk, f2 = packet_for(o, run, rub, opts)
        fs += f2
        packed.append(pk)
    ev = {"schema": "map-eval-run/1", "run": run_id, "created_at": now(), "rubric_version": rub["rubric_version"], "measure_version": MEASURE_VERSION,
          "plugin_version": plugin_version(), "mode": opts.get("mode") or "day", "passes": int(opts.get("passes") or 1), "model": opts.get("model"),
          "outputs": [p["id"] for p in packed], "skipped": [p for p in packed if p.get("status") == "skipped"], "trace": opts.get("trace")}
    (run / "evaluation.json").write_text(json.dumps(ev, indent=1), encoding="utf-8")
    return run, ev, fs


# ---------------------------------------------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["status", "save", "record", "measure", "trend", "anchors", "review", "export", "import", "tracker", "scoring-page"])
    ap.add_argument("--root", help="4.2.0, scoring-page: the folder the anchor set's files are in, when it is not where the set says")
    ap.add_argument("--since", help="4.2.0, export: the history lines from this date (YYYY-MM-DD) on")
    ap.add_argument("--sync", action="store_true", help="4.2.0, anchors: bring the owner's score sheet in step with the anchor set (no comparison)")
    ap.add_argument("file", nargs="?")
    ap.add_argument("--run")
    ap.add_argument("--read")
    ap.add_argument("--trace")
    ap.add_argument("--svg")
    ap.add_argument("--out")
    ap.add_argument("--set", dest="anchor_set")
    ap.add_argument("--owner")
    ap.add_argument("--days", type=int)
    a = ap.parse_args(argv)
    try:
        if a.op in ("status", "save", "record"):
            if not a.run:
                print("map_evaluate: --run DIR is needed (the run folder the pipeline printed)", file=sys.stderr)
                return gc.EXIT_USAGE
            run = Path(a.run)
            if not run.is_absolute() and not run.exists():
                run = evaluations_root() / a.run
            if a.op == "status":
                st = status(run)
                print(json.dumps(st, indent=1))
                for p in st["pending"]:
                    print(f"MAP_EVAL_PENDING: {p['read']} prompt={p['prompt']} answer={p['answer']}" + (f" problem={p['problem']}" if p.get("problem") else ""))
                print(f"MAP_EVAL_READY: {'yes' if st['ready'] else 'no'}")
                return gc.EXIT_OK
            if a.op == "save":
                if not (a.read and a.file):
                    print("map_evaluate: save needs --read ID and the file with the agent's JSON", file=sys.stderr)
                    return gc.EXIT_USAGE
                print(f"MAP_EVAL_SAVED: {save_read(run, a.read, Path(a.file))}")
                if a.read.endswith(":cold"):                 # 3.4.0 (Day 20 #27): the graded prompts are written as soon as the cold read is in
                    for q in status(run)["pending"]:
                        if q["output"] == a.read.partition(":")[0]:
                            print(f"MAP_EVAL_PENDING: {q['read']} prompt={q['prompt']} answer={q['answer']}")
                return gc.EXIT_OK
            r = record(run, Path(a.trace) if a.trace else None)
            print(json.dumps({k: v for k, v in r.items() if k != "findings"}, indent=1, default=str))
            for f in r["findings"]:
                if f["severity"] != "INFO":
                    print(f"{f['id']} {f['severity']}: {f['message']}")
            print(f"MAP_EVAL_REPORT: {r['report']}")
            if r.get("trace"):
                print(f"MAP_EVAL_TRACE_HTML: {r['trace']['html']}")
            return gc.EXIT_OK
        if a.op == "measure":
            if not a.file:
                return gc.EXIT_USAGE
            m, fs = measure(Path(a.file), Path(a.svg) if a.svg else None)
            print(json.dumps({"measures": m, "findings": fs}, indent=1, default=str))
            return gc.EXIT_OK
        if a.op == "trend":
            out = Path(a.out) if a.out else evaluations_root(create=True) / "trend.html"
            out.write_text(trend_html(history(), load_rubric()), encoding="utf-8")
            print(f"MAP_EVAL_TREND: {out}")
            return gc.EXIT_OK
        if a.op == "export":                                   # 4.2.0: scores that outlive a cloud session
            out = Path(a.out or a.file or (evaluations_root(create=True) / "evaluation-handback.json"))
            r = export_history(out, a.run, a.since)
            print(json.dumps(r, indent=1))
            print(f"MAP_EVAL_HANDBACK: {r['file']}")
            return gc.EXIT_OK
        if a.op == "import":
            if not a.file:
                print("map_evaluate: import needs the hand-back file (map_evaluate.py export writes it)", file=sys.stderr)
                return gc.EXIT_USAGE
            print(json.dumps(import_history(Path(a.file)), indent=1))
            return gc.EXIT_OK
        if a.op == "tracker":
            rows = tracker_lines(a.run)
            print(json.dumps(rows, indent=1))
            for r in rows:
                print(f"MAP_EVAL_TRACKER: {r['output']}: {r['text']}")
            return gc.EXIT_OK
        if a.op == "scoring-page":
            if not (a.anchor_set and (a.out or a.file)):
                print("map_evaluate: scoring-page needs --set anchors.yaml and --out page.html (--owner owner-scores.yaml fills what is scored)", file=sys.stderr)
                return gc.EXIT_USAGE
            r = scoring_page(Path(a.anchor_set), Path(a.owner) if a.owner else None, Path(a.out or a.file), Path(a.root) if a.root else None)
            print(json.dumps(r, indent=1))
            print(f"MAP_EVAL_SCORING_PAGE: {r['page']}")
            return gc.EXIT_OK
        if a.op == "anchors" and a.sync:
            if not (a.anchor_set and a.owner):
                print("map_evaluate: anchors --sync needs --set anchors.yaml and --owner owner-scores.yaml", file=sys.stderr)
                return gc.EXIT_USAGE
            print(json.dumps(sync_owner(Path(a.anchor_set), Path(a.owner)), indent=1))
            return gc.EXIT_OK
        if a.op == "anchors":
            if not (a.anchor_set and a.owner):
                print("map_evaluate: anchors needs --set anchors.yaml and --owner owner-scores.yaml", file=sys.stderr)
                return gc.EXIT_USAGE
            print(json.dumps(anchors_agreement(Path(a.anchor_set), Path(a.owner)), indent=1))
            return gc.EXIT_OK
        if a.op == "review":
            print(json.dumps(review(a.days, Path(a.owner) if a.owner else None, Path(a.anchor_set) if a.anchor_set else None), indent=1))
            return gc.EXIT_OK
    except EvalError as ex:
        print(f"map_evaluate: {ex.fid}: {ex}", file=sys.stderr)
        return gc.EXIT_LINT
    return gc.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
