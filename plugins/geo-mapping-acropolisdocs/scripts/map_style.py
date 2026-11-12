#!/usr/bin/env python3
"""map_style: style packs (1.4.0, R2-PACK). A pack is a named, versioned bundle that sets how a map looks: palette (tokens), type (fonts),
ornaments (frame, title box, north arrow, scale bar, legend frame) and treatments (paper, water-lining, hill and place glyphs, line character).

Every map has a pack. With none named it is `default`: the map exactly as before 1.4.0, the brand's palette and Inter, today's plain elements.
The brand is the default pack's style; its identity (logo, stamps, hidden accreditation, footer credit, edition lock) is kept under every pack,
in both editions. A pack that replaces the brand's colours and fonts is used only when the person names it: the request is the approval.

Where packs live: bundled under <plugin>/styles/<id>/ (pack.yaml, fonts/, assets/, glyphs/, LICENSES/), and the person's own under
<working folder>/styles/<id>/ with the same layout and schema (schemas/style-pack.schema.json).

Resolution, in the plan stage only (map_plan.prepare): the pack's tokens (the brand's for `palette: brand`, the brand's changed by `derive:` for
`palette: brand-derived`), then the look's generated colours (map_look seeds its ramps from the resolved `emphasis`), then the layer's palette.
The result is plan.tokens and plan.style, so every renderer, legend and lint downstream reads the resolved pack and needs no pack code.

    map_style.py list                       the packs this plugin and this person have
    map_style.py show ID                    the resolved pack (its record, faces, elements and treatments)
"""
from __future__ import annotations

import argparse
import base64
import copy
import functools
import hashlib
import io
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

DEFAULT = "default"
FONT_LICENCES = {"OFL-1.1": "SIL Open Font License 1.1", "Apache-2.0": "Apache License 2.0", "UFL-1.0": "Ubuntu Font Licence 1.0"}
ROLES = ("title", "label", "body")
FF = "font-family"          # the pack's families are data (pack.yaml), never written into the code
GENERIC = {"serif", "sans-serif", "monospace", "cursive", "system-ui", "fantasy"}


class StyleError(Exception):
    def __init__(self, message: str, rule: str = "MAP-STYLE-003"):
        super().__init__(message)
        self.rule = rule


def finding(fid: str, sev: str, msg: str, detail=None) -> dict:
    d = {"id": fid, "severity": sev, "message": msg}
    if detail is not None:
        d["detail"] = detail
    return d


@dataclass
class Style:
    id: str
    version: int
    title: str
    dir: Path
    data: dict
    sha256: str
    source: str                                  # bundled | person
    type: dict = field(default_factory=dict)     # role -> {family, file, style, generic, licence, licence_id}; empty: the brand's Inter
    findings: list = field(default_factory=list)

    @property
    def is_default(self) -> bool:
        return self.id == DEFAULT

    @property
    def palette(self) -> str:
        return self.data.get("palette", "brand")

    @property
    def fonts(self) -> str:
        return self.data.get("fonts", "brand")

    @property
    def elements(self) -> dict:
        return dict(self.data.get("elements") or {})

    @property
    def treatments(self) -> dict:
        return dict(self.data.get("treatments") or {})

    @property
    def web(self) -> str:
        return self.data.get("web", "full")

    def families(self) -> set[str]:
        """The font families a map in this pack may use and must embed: each role's face, Inter where a role keeps the brand's typeface."""
        fams = set()
        for r in ROLES:
            f = self.type.get(r) or self.type.get("body")
            fams.add(gc.FONT_FAMILY if (f is None or f.get("brand")) else f["family"])
        return fams

    def record(self) -> dict:
        """What the build report and provenance carry (R2-PACK 4.2)."""
        return {"pack": self.id, "version": self.version, "sha256": self.sha256, "source": self.source,
                "replaces_palette": self.palette != "brand", "replaces_fonts": self.fonts != "brand",
                "faces": {r: {"family": f["family"], "licence": FONT_LICENCES.get(f.get("licence_id") or "", f.get("licence_id"))} for r, f in self.type.items() if not f.get("brand")}}

    def path(self, rel: str) -> Path:
        return self.dir / rel


# ---------------------------------------------------------------------------------------------------------------- where packs live
def bundled_root() -> Path:
    return gc.plugin_root() / "styles"


def person_root() -> Path:
    return gc.workdir() / "styles"


def pack_dir(pid: str) -> tuple[Path, str] | None:
    for root, src in ((person_root(), "person"), (bundled_root(), "bundled")):
        d = root / pid
        if (d / "pack.yaml").is_file():
            return d, src
    return None


def list_packs() -> list[dict]:
    out, seen = [], set()
    for root, src in ((bundled_root(), "bundled"), (person_root(), "person")):
        if not root.is_dir():
            continue
        for d in sorted(p for p in root.iterdir() if (p / "pack.yaml").is_file()):
            if d.name in seen:
                continue
            seen.add(d.name)
            try:
                y = gc.load_yaml(d / "pack.yaml") or {}
            except Exception:  # noqa: BLE001
                y = {}
            out.append({"id": d.name, "title": y.get("title"), "intent": y.get("intent"), "palette": y.get("palette"), "fonts": y.get("fonts"),
                        "web": y.get("web"), "source": src, "preview": str(d / "preview.png") if (d / "preview.png").is_file() else None})
    return out


def pack_of(spec: dict) -> str:
    """The pack a MapSpec asks for: style.pack, else `default`."""
    st = spec.get("style") if isinstance(spec.get("style"), dict) else {}
    return str(st.get("pack") or DEFAULT)


@functools.lru_cache(maxsize=1)
def _schema() -> dict:
    return json.loads((gc.plugin_root() / "schemas" / "style-pack.schema.json").read_text())


def sha256_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def default_pack_data() -> dict:
    """The `default` pack, as tools/import_brand.py writes it (styles/default/pack.yaml): the brand's palette and fonts, today's elements."""
    return {"id": "default", "version": 1, "title": "Default",
            "intent": "A normal map: the brand's palette and typeface, its plain frame, title, north arrow and scale bar.",
            "generated": "tools/import_brand.py: the brand is this pack's style; never edited by hand",
            "palette": "brand", "fonts": "brand", "web": "full"}


@functools.lru_cache(maxsize=32)
def _load_cached(pid: str, dir_: str, mtime: float) -> Style:
    return _load(pid, Path(dir_))


def load(pid: str) -> Style:
    """The pack by id, checked (MAP-STYLE-001 schema and file hashes, MAP-STYLE-004 font licences). Unknown: MAP-STYLE-003."""
    if pid == DEFAULT and pack_dir(DEFAULT) is None:                 # a source tree without the generated file still has `default`
        return Style(id=DEFAULT, version=1, title="Default", dir=bundled_root() / DEFAULT, data=default_pack_data(),
                     sha256=hashlib.sha256(json.dumps(default_pack_data(), sort_keys=True).encode()).hexdigest(), source="bundled")
    found = pack_dir(pid)
    if found is None:
        known = ", ".join(p["id"] for p in list_packs()) or "default"
        raise StyleError(f"no style pack '{pid}'; the packs are {known}", "MAP-STYLE-003")
    d, _src = found
    return _load_cached(pid, str(d), (d / "pack.yaml").stat().st_mtime)


def _load(pid: str, d: Path) -> Style:
    src = "person" if d.parent == person_root() else "bundled"
    raw = (d / "pack.yaml").read_bytes()
    data = gc.load_yaml(d / "pack.yaml") or {}
    import jsonschema
    errs = sorted(jsonschema.Draft202012Validator(_schema()).iter_errors(data), key=lambda e: list(e.path))
    if errs:
        e = errs[0]
        raise StyleError(f"style pack '{pid}': pack.yaml {'/'.join(map(str, e.path)) or '(root)'}: {e.message}", "MAP-STYLE-001")
    if data["id"] != pid:
        raise StyleError(f"style pack folder '{pid}' holds pack '{data['id']}'", "MAP-STYLE-001")
    for rel, want in (data.get("files") or {}).items():
        p = d / rel
        if not p.is_file():
            raise StyleError(f"style pack '{pid}': {rel} is listed but missing", "MAP-STYLE-001")
        if sha256_file(p) != want:
            raise StyleError(f"style pack '{pid}': {rel} does not match its SHA-256 in pack.yaml", "MAP-STYLE-001")
    faces: dict = {}
    if data.get("fonts") == "own":
        for r, face in (data.get("type") or {}).items():
            face = dict(face)
            if face.get("family") == gc.FONT_FAMILY and not face.get("file"):
                face["brand"] = True                         # the brand's Inter for this role (editorial's body)
                faces[r] = face
                continue
            if not face.get("file"):
                raise StyleError(f"style pack '{pid}': the {r} face {face.get('family')} names no file", "MAP-STYLE-001")
            fp = d / face["file"]
            if not fp.is_file():
                raise StyleError(f"style pack '{pid}': the {r} face file {face['file']} is missing", "MAP-STYLE-001")
            if face.get("sha256") and sha256_file(fp) != face["sha256"]:
                raise StyleError(f"style pack '{pid}': {face['file']} does not match its SHA-256", "MAP-STYLE-001")
            lic = face.get("licence")
            if not lic or not (d / lic).is_file():
                raise StyleError(f"style pack '{pid}': the font {face['family']} ({face['file']}) has no licence file in the pack", "MAP-STYLE-004")
            if face.get("licence_id") not in FONT_LICENCES:
                raise StyleError(f"style pack '{pid}': the font {face['family']} is under '{face.get('licence_id')}', which is not on the allow-list "
                                 f"({', '.join(FONT_LICENCES.values())})", "MAP-STYLE-004")
            text = (d / lic).read_text(encoding="utf-8", errors="replace")
            if face["licence_id"] == "OFL-1.1" and "SIL OPEN FONT LICENSE" not in text.upper():
                raise StyleError(f"style pack '{pid}': {lic} is not the SIL Open Font License text", "MAP-STYLE-004")
            face["file"] = str(fp)
            faces[r] = face
    return Style(id=pid, version=int(data["version"]), title=data["title"], dir=d, data=data, sha256=hashlib.sha256(raw).hexdigest(), source=src, type=faces)


# ---------------------------------------------------------------------------------------------------------------- palette
def _walk_hex(v):
    stack = [v]
    while stack:
        x = stack.pop()
        if isinstance(x, str) and x.startswith("#"):
            yield x
        elif isinstance(x, list):
            stack.extend(x)
        elif isinstance(x, dict):
            stack.extend(x.values())


def mode_tokens(style: Style, brand_tokens_modes: dict, mode: str) -> dict:
    """The base tokens of a mode under this pack, before the look: the brand's, the pack's own, or the brand's changed by `derive`."""
    if style.palette == "brand":
        return copy.deepcopy(brand_tokens_modes[mode])
    if style.palette == "own":
        modes = style.data["modes"]
        m = modes.get(mode) or modes["print"]
        if isinstance(m, dict) and m.get("inherit"):
            m = modes[m["inherit"]]
        return copy.deepcopy(m)
    return derive_tokens(brand_tokens_modes[mode], style.data.get("derive") or {}, mode)


def derive_tokens(t: dict, d: dict, mode: str = "print") -> dict:
    """palette: brand-derived. The brand's tokens with a chroma cap on the data colours, the ground and context desaturated, and the ground
    white or the pack's paper tint (print only: a dark ground keeps the brand's). Deterministic, so the lint recomputes the same colours."""
    import colorlib as cl
    out = copy.deepcopy(t)
    cap = d.get("chroma_cap")

    def capped(h: str) -> str:
        return cap_color(h, cap)

    def desat(h: str, f: float) -> str:
        hue, light, sat = cl.hls(h)
        return cl.from_hls(hue, light, sat * (1.0 - f))

    for key in ("categorical", "sequential_ramp", "diverging_ramp"):
        if isinstance(out.get(key), list):
            out[key] = [capped(x) for x in out[key]]
    if cap is not None and isinstance(out.get("categorical"), list):
        # capping chroma can bring two categories together: one too close (under colour-vision simulation) to an earlier one is dropped,
        # as the brand import drops them (MAP-STYLE-005 checks the same rule)
        kept = []
        for c in out["categorical"]:
            if not kept or min(cl.cvd_min_delta_e(c, k) for k in kept) >= 10.0 or len(kept) + (len(out["categorical"]) - len(kept)) <= 3:
                kept.append(c)
        out["categorical"] = kept
    out["emphasis"] = capped(out["emphasis"])
    f = float(d.get("context_desaturate") or 0)
    if f:
        for key in ("land", "water", "frame", "legend_frame", "legend_bg", "no_data"):
            if isinstance(out.get(key), str):
                out[key] = desat(out[key], f)
        out["neutral_ramp"] = [desat(x, f) for x in out["neutral_ramp"]]
    ground = d.get("ground", "brand")
    if mode == "print" and ground in ("white", "paper"):
        tok = d.get("tokens") or {}
        g = t["background"] if ground == "white" else tok["paper"]          # white: the brand's print ground; paper: the pack's (schema)
        out["background"] = g
        out["label_halo"] = g
        out["land"] = g if ground == "white" else tok.get("paper_land", g)
    for k, v in (d.get("tokens") or {}).items():
        out[k] = v
    return out


def cap_color(h: str, cap: float | None) -> str:
    """The colour with its OKLab chroma held to `cap` (lightness and hue kept); unchanged when it is under the cap or there is none."""
    import colorlib as cl
    if cap is None or not isinstance(h, str) or not h.startswith("#"):
        return h
    L, a, b = cl.oklab(h)
    c = (a * a + b * b) ** 0.5
    if c <= cap:
        return cl.norm_hex(h)
    k = cap / c if c else 0.0
    return cl.from_oklab(L, a * k, b * k)


def after_look(style: Style, tokens: dict) -> dict:
    """A brand-derived pack's chroma cap on the colours the look generates (its ramps): applied after map_look, so a generated ramp keeps
    the pack's character (monochrome stays grey). The brand lint allows the same capped colours."""
    cap = ((style.data.get("derive") or {}).get("chroma_cap")) if style is not None and style.palette == "brand-derived" else None
    if cap is None:
        return tokens
    t = dict(tokens)
    t["_chroma_cap"] = cap
    for k in ("sequential_ramp", "diverging_ramp"):
        if isinstance(t.get(k), list):
            t[k] = [cap_color(c, cap) for c in t[k]]
    return t


def palette_modes(style: Style, brand_tokens_modes: dict) -> dict:
    """Every mode's base tokens under this pack: what MAP-BRAND-001 allows (with the colours the looks derive from them)."""
    return {m: mode_tokens(style, brand_tokens_modes, m) for m in brand_tokens_modes}


def token(t: dict, name: str | None, fallback: str) -> str:
    """A pack ink token (water_line, hill_ink, ornament ...) or the named fallback token."""
    if name and isinstance(t.get(name), str):
        return t[name]
    return t[fallback]


# ---------------------------------------------------------------------------------------------------------------- elements
ELEMENT_KEYS = {"frame": ("style", "bars", "grid_mm"), "title": ("box",), "north_arrow": ("style", "points"), "scale_bar": ("style",), "legend": ("frame",),
                "callouts": ("weight",)}


def apply_elements(spec: dict, style: Style) -> list[str]:
    """The pack's ornament settings become the spec's element defaults (in memory only): a key the MapSpec sets itself is kept. -> notes."""
    notes = []
    els = spec.setdefault("elements", {})
    for name, cfg in style.elements.items():
        if name == "title_area":                         # large: the title set a third larger (editorial)
            if cfg == "large":
                ti = els.get("title") if isinstance(els.get("title"), dict) else {}
                if "scale" not in ti:
                    els["title"] = {**ti, "scale": 1.35, "on": ti.get("on", True)}
                    notes.append("title: scale=1.35")
            continue
        if not isinstance(cfg, dict):
            continue
        cur = els.get(name)
        cur = dict(cur) if isinstance(cur, dict) else {}
        set_ = []
        for k, v in cfg.items():
            if k not in cur:
                cur[k] = v
                set_.append(f"{k}={v}")
        if set_:
            cur.setdefault("on", True if name not in ("frame",) else cur.get("on", True))
            els[name] = cur
            notes.append(f"{name}: {', '.join(set_)}")
    return notes


# ---------------------------------------------------------------------------------------------------------------- fonts
def _subset_woff2(path: str, chars: str) -> bytes:
    import logging
    from fontTools import subset
    logging.getLogger("fontTools.subset").setLevel(logging.ERROR)
    from fontTools.ttLib import TTFont
    opts = subset.Options()
    opts.layout_features = ["*"]                  # OpenType features kept (ligatures, old-style figures): spike S3, 1.4.0
    opts.name_IDs = ["*"]
    opts.name_languages = ["*"]
    opts.notdef_outline = True
    opts.flavor = "woff2"
    opts.hinting = False
    f = TTFont(path)
    s = subset.Subsetter(opts)
    s.populate(text=chars + " ")
    s.subset(f)
    buf = io.BytesIO()
    f.flavor = "woff2"
    f.save(buf)
    return buf.getvalue()


@functools.lru_cache(maxsize=64)
def subset_b64(path: str, chars: str) -> str:
    return base64.b64encode(_subset_woff2(path, chars)).decode("ascii")


def font_css(style: Style, chars: str) -> str:
    """The @font-face of each pack face (a WOFF2 subset of the glyphs the sheet uses) and the role rules: `.gm-t` is the body face,
    `.gm-r-title` and `.gm-r-label` the others (svg_draw.text adds those classes). Inter is embedded as today when a role keeps it."""
    import brandlib as bl
    faces, rules, seen = [], [], set()
    chars = "".join(sorted(set(chars)))
    need_inter = False
    for r in ROLES:
        f = style.type.get(r) or style.type.get("body")
        if f is None or f.get("brand"):
            need_inter = True
            continue
        key = (f["family"], f.get("style", "normal"), f["file"])
        if key not in seen:
            seen.add(key)
            faces.append(f"@font-face{{{FF}:'{f['family']}';font-style:{f.get('style', 'normal')};font-weight:100 900;"
                         f"src:url(data:font/woff2;base64,{subset_b64(f['file'], chars)}) format('woff2');}}")
    for r in ("body", "title", "label"):              # body first: a role rule after it wins on the same specificity
        f = style.type.get(r) or style.type.get("body")
        fam = gc.FONT_FAMILY if (f is None or f.get("brand")) else f["family"]
        gen = (f or {}).get("generic") or "sans-serif"
        fst = (f or {}).get("style", "normal")
        sel = ".gm-t" if r == "body" else f".gm-r-{r}"
        extra = "font-kerning:normal;" if r == "body" else ""
        rules.append(f"{sel}{{{FF}:'{fam}',{gen};font-style:{fst};{extra}}}")
    return (bl.inter_font_face_css() if need_inter else "") + "".join(faces) + "".join(rules)


def text_chars(svg_body: str) -> str:
    """Every character a sheet's text elements draw (what the font subsets keep)."""
    import html as htmllib
    import re
    return "".join(htmllib.unescape(m) for m in re.findall(r"<text\b[^>]*>([^<]*)</text>", svg_body))


def pdf_font_names(style) -> list[str] | None:
    """The names a PDF of this pack may embed its fonts under (each face's family and PostScript names, Inter where a role keeps it), for
    MAP-OUT-002; None under a pack that keeps the brand's fonts (Inter only, as before 1.4.0)."""
    if style is None or style.fonts == "brand":
        return None
    out = set()
    for r in ROLES:
        f = style.type.get(r) or style.type.get("body")
        if f is None or f.get("brand"):
            out.add(gc.FONT_FAMILY)
            continue
        out.add(f["family"])
        try:
            from fontTools.ttLib import TTFont
            nm = TTFont(f["file"], lazy=True)["name"]
            for nid in (1, 4, 6, 16):
                v = nm.getDebugName(nid)
                if v:
                    out.add(v)
        except Exception:  # noqa: BLE001
            pass
    return sorted(out)


# ---------------------------------------------------------------------------------------------------------------- plan stage
def resolve_for_plan(spec: dict, brand_pack: dict, mode: str) -> tuple[Style, dict, list[str]]:
    """-> (style, base tokens for the mode, notes). The pack is fixed here for the whole build (4.2)."""
    import textmeasure as tm
    st = load(pack_of(spec))
    base = mode_tokens(st, brand_pack["tokens"]["modes"], mode)
    notes = []
    if not st.is_default:
        notes.append(f"style pack '{st.id}' v{st.version}: palette {st.palette}, fonts {st.fonts} (asked for by the MapSpec; the brand's logo, stamps, "
                     "hidden accreditation and footer credit are kept)")
        el_notes = apply_elements(spec, st)
        if el_notes:
            notes.append("style pack elements: " + "; ".join(el_notes))
    tm.use_type({r: f for r, f in st.type.items() if not f.get("brand")} or None)
    return st, base, notes


# ---------------------------------------------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="op", required=True)
    sub.add_parser("list")
    s = sub.add_parser("show")
    s.add_argument("id")
    a = ap.parse_args(argv)
    if a.op == "list":
        print(json.dumps(list_packs(), indent=2))
        return 0
    try:
        st = load(a.id)
    except StyleError as ex:
        print(json.dumps({"ok": False, "findings": [finding(ex.rule, "BLOCK", str(ex))]}, indent=2))
        return gc.EXIT_LINT
    print(json.dumps({"ok": True, "record": st.record(), "palette": st.palette, "fonts": st.fonts, "elements": st.elements,
                      "treatments": st.treatments, "web": st.web, "faces": {r: {k: v for k, v in f.items() if k != "file"} for r, f in st.type.items()}}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
