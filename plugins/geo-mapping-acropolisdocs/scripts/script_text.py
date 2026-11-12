#!/usr/bin/env python3
"""script_text: labels in scripts Inter does not cover (1.5.0, R3-SCRIPT).

Inter covers Latin, Greek and Cyrillic. A label in another script (Arabic, Hebrew, Devanagari, Chinese, Japanese, Korean, ...) is set in the
Noto family for its script (data/script-fonts.yaml), shaped with HarfBuzz and drawn as outlined paths, the words kept in <title> and an
aria-label, so every route (the SVG itself, the PDF, the PNG, a browser) shows the same letters and no font has to travel with the map
(spike S4, docs/spikes/S4-script-shaping.md). Without the font, or without uharfbuzz for a script that needs shaping (right-to-left and joining
scripts, Indic), the caller falls back to the English name and says which scripts were left out (MAP-LOC-003).

    script_text.py fetch arab,hebr,deva,hani        what `doctor --fonts` runs: the pinned files into the font cache, SHA-256 and OFL checked
    script_text.py status                            which script fonts are cached here
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

INTER_SCRIPTS = {"Latn", "Grek", "Cyrl", "Zyyy", "Zinh", "Zzzz"}
INTER = "inter"                                    # the key of the bundled Inter range files (1.5.0): Latin Extended, Greek, Cyrillic, Vietnamese
INTER_EXTRA = ("latin-ext", "greek", "greek-ext", "cyrillic", "cyrillic-ext", "vietnamese")
LANG_SCRIPT = {"el": "Grek", "ru": "Cyrl", "uk": "Cyrl", "ja": "Jpan", "zh": "Hani", "zht": "Hani", "ko": "Hang", "ar": "Arab", "fa": "Arab",
               "ur": "Arab", "he": "Hebr", "hi": "Deva", "bn": "Beng"}


@functools.lru_cache(maxsize=1)
def registry() -> dict:
    return gc.load_yaml(gc.plugin_root() / "data" / "script-fonts.yaml")


def font_dir() -> Path:
    return gc.outputs_dir() / "fonts" / "noto"


_LANG: list = [None]


def use_lang(lang: str | None) -> None:
    """The language of the names being set (ja, zh, zht, ko pick the CJK face for Han characters)."""
    _LANG[0] = lang


def scripts_in(s: str) -> set[str]:
    """The scripts in `s` that no Inter file the plugin bundles covers (they need a Noto face)."""
    s = str(s)
    if all(ord(ch) < 0x0530 or 0x1E00 <= ord(ch) < 0x2C00 for ch in s):          # Latin, Greek, Cyrillic, punctuation: Inter
        return set()
    from fontTools import unicodedata as ud
    return {ud.script(ch) for ch in s} - INTER_SCRIPTS


@functools.lru_cache(maxsize=1)
def _inter_files() -> list:
    """[(path, codepoints)] of the bundled Inter files: the main (Latin) file, which the SVG embeds, first."""
    from fontTools.ttLib import TTFont
    d = gc.plugin_root() / "assets" / "fonts" / "inter"
    out = []
    for name in ("latin",) + INTER_EXTRA:
        p = d / f"inter-{name}-wght-normal.woff2"
        if p.is_file():
            out.append((p, frozenset(TTFont(str(p), lazy=True).getBestCmap())))
    return out


def outside_embedded_inter(s: str) -> bool:
    """Whether `s` has a character the embedded Inter (the Latin file) lacks: Greek, Cyrillic, Latin Extended. The SVG embeds only that file, so
    such a label is outlined from the Inter range files rather than left to a renderer's fallback font (1.5.0)."""
    s = str(s)
    if s.isascii():
        return False
    fs = _inter_files()
    return bool(fs) and any(ord(ch) not in fs[0][1] and not ch.isspace() for ch in s)


def needs_font(s: str) -> bool:
    return bool(scripts_in(s)) or outside_embedded_inter(s)


def key_for(script: str, lang: str | None = None) -> str | None:
    """The registry key of the font for a script (Hani by language: ja, zh, zht, ko)."""
    reg = registry()
    lang = lang or _LANG[0]
    if lang:
        for k, f in reg["fonts"].items():
            if lang in (f.get("lang") or []) and script in f["scripts"]:
                return k
    for k, f in reg["fonts"].items():
        if script in f["scripts"]:
            return k
    return None


def font_path(key: str) -> Path | None:
    f = registry()["fonts"].get(key)
    if not f:
        return None
    p = font_dir() / Path(f["path"]).name
    return p if p.is_file() else None


def shaper() -> bool:
    try:
        import uharfbuzz  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def plan_for(s: str, lang: str | None = None) -> tuple[str | None, list[str]]:
    """(font key, scripts that cannot be drawn here) for a string; (None, []) when Inter covers it."""
    sc = scripts_in(s)
    if not sc:
        if outside_embedded_inter(s):
            cov = set().union(*(c for _p, c in _inter_files()))
            miss = sorted({ch for ch in str(s) if ord(ch) not in cov and not ch.isspace()})
            return (INTER, []) if not miss else (None, ["Inter range"])
        return None, []
    keys = {x: key_for(x, lang) for x in sc}
    missing = [x for x, k in keys.items() if k is None or font_path(k) is None]
    if missing:
        return None, sorted(missing)
    ks = set(keys.values())
    if len(ks) > 1:                                  # a label in two non-Inter scripts at once: the CJK font when it holds both (kana and kanji)
        cjk = [k for k in ks if len(registry()["fonts"][k]["scripts"]) > 1]
        if not cjk:
            return None, sorted(sc)
        ks = {cjk[0]}
    k = ks.pop()
    if registry()["fonts"][k].get("shaping") == "complex" and not shaper():
        return None, sorted(sc)
    return k, []


@functools.lru_cache(maxsize=16)
def _font(key: str):
    from fontTools.ttLib import TTFont
    return TTFont(str(font_path(key)), lazy=True)


@functools.lru_cache(maxsize=16)
def _hb(key: str):
    import uharfbuzz as hb
    return hb.Font(hb.Face(hb.Blob(Path(font_path(key)).read_bytes())))


def _layout(s: str, key: str) -> tuple[list[tuple[str, float, float, float]], int]:
    """[(glyph name, x advance, x offset, y offset)] in font units and the units per em, shaped by HarfBuzz when it is installed (bidi and
    joining included: HarfBuzz orders a right-to-left run visually), else by the cmap and the advances."""
    f = _font(key)
    upm = f["head"].unitsPerEm
    order = f.getGlyphOrder()
    if shaper():
        import uharfbuzz as hb
        b = hb.Buffer()
        b.add_str(s)
        b.guess_segment_properties()
        hb.shape(_hb(key), b, {"kern": True, "liga": True})
        return [(order[i.codepoint], p.x_advance, p.x_offset, p.y_offset) for i, p in zip(b.glyph_infos, b.glyph_positions)], upm
    cmap = f.getBestCmap()
    hm = f["hmtx"]
    out = []
    for ch in s:
        g = cmap.get(ord(ch), ".notdef")
        out.append((g, hm[g][0] if g in hm.metrics else upm // 2, 0, 0))
    return out, upm


@functools.lru_cache(maxsize=32)
def _inter_face(path: str, weight: int):
    """An Inter range file at a weight (its variable wght axis instanced), as (TTFont, glyph set, cmap, hmtx, units per em)."""
    from fontTools.ttLib import TTFont
    f = TTFont(path)
    if "fvar" in f:
        from fontTools.varLib import instancer
        f = instancer.instantiateVariableFont(f, {"wght": max(100, min(900, int(weight)))})
    return f, f.getGlyphSet(), f.getBestCmap(), f["hmtx"], f["head"].unitsPerEm


def _inter_runs(s: str, weight: int) -> list:
    """[(face, glyph name, advance in font units)] for each character, from the first Inter file that has it (no shaping is needed for Latin,
    Greek and Cyrillic; kerning across files is not applied)."""
    out = []
    files = _inter_files()
    for ch in str(s):
        for p, cov in files:
            if ord(ch) in cov:
                face = _inter_face(str(p), weight)
                g = face[2][ord(ch)]
                out.append((face, g, face[3][g][0]))
                break
        else:
            face = _inter_face(str(files[0][0]), weight)
            out.append((face, ".notdef", face[4] // 2))
    return out


def width_mm(s: str, size_mm: float, key: str, weight: int = 400) -> float:
    if key == INTER:
        return sum(a * size_mm / face[4] for face, _g, a in _inter_runs(s, weight))
    lay, upm = _layout(s, key)
    return sum(a for _g, a, _dx, _dy in lay) * size_mm / upm


def path_d(s: str, x: float, y: float, size_mm: float, key: str, anchor: str = "start", weight: int = 400, tracking: float = 0.0) -> tuple[str, float]:
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    if key == INTER:
        runs = _inter_runs(s, weight)
        total = sum(a * size_mm / face[4] + tracking for face, _g, a in runs) - (tracking if runs else 0.0)
        cx = x - (total / 2 if anchor == "middle" else total if anchor == "end" else 0.0)
        parts = []
        for face, g, a in runs:
            k = size_mm / face[4]
            if g in face[1]:
                pen = SVGPathPen(face[1], ntos=lambda v: (f"{v:.3f}".rstrip("0").rstrip(".") or "0"))     # its own glyph set: composites
                face[1][g].draw(TransformPen(pen, (k, 0, 0, -k, cx, y)))
                parts.append(pen.getCommands())
            cx += a * k + tracking
        return "".join(parts), total
    lay, upm = _layout(s, key)
    k = size_mm / upm
    total = sum(a for _g, a, _dx, _dy in lay) * k
    x0 = x - (total / 2 if anchor == "middle" else total if anchor == "end" else 0.0)
    gs = _font(key).getGlyphSet()
    pen = SVGPathPen(gs, ntos=lambda v: (f"{v:.3f}".rstrip("0").rstrip(".") or "0"))
    cx = x0
    for g, a, dx, dy in lay:
        if g in gs:
            gs[g].draw(TransformPen(pen, (k, 0, 0, -k, cx + dx * k, y - dy * k)))
        cx += a * k
    return pen.getCommands(), total


def svg_label(s: str, x: float, y: float, size_mm: float, key: str, fill: str, *, anchor: str = "start", halo: str | None = None,
              halo_w: float = 0.0, extra: str = "", transform: str = "", weight: int = 400, tracking: float = 0.0) -> str:
    """The label as outlined paths: a halo path (hidden from screen readers) under a group named by the words."""
    from xml.sax.saxutils import escape, quoteattr
    d, _w = path_d(s, x, y, size_mm, key, anchor, weight, tracking)
    fam = "Inter" if key == INTER else registry()["fonts"][key]["family"]
    tr = f' transform="{transform}"' if transform else ""
    out = ""
    if halo and halo_w > 0:
        out += (f'<path class="gm-outlined-halo" aria-hidden="true" d="{d or "M0 0"}" fill="{halo}" stroke="{halo}" stroke-width="{halo_w:.3f}" '
                f'stroke-linejoin="round"{tr}/>')
    out += (f'<g class="gm-outlined gm-script" role="img" aria-label={quoteattr(s)} data-script-font="{escape(fam)}"{extra}{tr}>'
            f'<title>{escape(s)}</title><path d="{d or "M0 0"}" fill="{fill}"/></g>')
    return out


# ------------------------------------------------------------------------------------------------------------------- doctor --fonts
def fetch(keys: list[str], *, http=None, refresh: bool = False) -> list[dict]:
    """The pinned Noto files into the font cache: SHA-256 checked against the registry, the source's OFL text fetched and checked beside them.
    -> one record a font (MAP-LOC-004 INFO for each one fetched)."""
    reg = registry()
    if http is None:
        import ogc_http
        http = ogc_http.SafeHTTP(limits={"timeout_s": 120, "max_bytes": 20_000_000})
    out = []
    font_dir().mkdir(parents=True, exist_ok=True)
    for k0 in keys:
        k = reg.get("aliases", {}).get(k0.lower(), k0.lower())
        f = reg["fonts"].get(k)
        if not f:
            out.append({"key": k0, "ok": False, "error": f"no font for '{k0}' (one of {', '.join(sorted(reg['fonts']))})"})
            continue
        src = reg["sources"][f["source"]]
        dest = font_dir() / Path(f["path"]).name
        lic = font_dir() / f"OFL-{f['source']}.txt"
        if dest.is_file() and lic.is_file() and not refresh and hashlib.sha256(dest.read_bytes()).hexdigest() == f["sha256"]:
            out.append({"key": k, "family": f["family"], "ok": True, "cached": True, "path": str(dest)})
            continue
        if not lic.is_file() or refresh:
            r = http.get(src["base"] + src["licence_file"], max_bytes=1_000_000)
            if r.status != 200 or hashlib.sha256(r.content).hexdigest() != src["licence_sha256"] or reg["licence"]["check"].encode() not in r.content:
                out.append({"key": k, "ok": False, "error": "the licence text did not match the pinned OFL; nothing was kept"})
                continue
            lic.write_bytes(r.content)
        r = http.get(src["base"] + f["path"], max_bytes=20_000_000)
        sha = hashlib.sha256(r.content).hexdigest()
        if r.status != 200 or sha != f["sha256"]:
            out.append({"key": k, "ok": False, "error": f"the file did not match its pinned SHA-256 (HTTP {r.status}); nothing was kept"})
            continue
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(r.content)
        tmp.replace(dest)
        out.append({"key": k, "family": f["family"], "ok": True, "cached": False, "path": str(dest), "sha256": sha, "licence": str(lic),
                    "finding": {"id": "MAP-LOC-004", "severity": "INFO",
                                "message": f"{f['family']} fetched for {', '.join(f['scripts'])} ({len(r.content) / 1e6:.1f} MB, SHA-256 and SIL OFL 1.1 checked)"}})
    return out


def status() -> dict:
    reg = registry()
    return {k: {"family": f["family"], "scripts": f["scripts"], "cached": font_path(k) is not None} for k, f in reg["fonts"].items()} | {
        "_shaper": {"uharfbuzz": shaper()}}


def fonts_used(texts: list[str]) -> list[dict]:
    """The script fonts a map drew with (for the terms register): family, file, licence."""
    keys = set()
    for s in texts:
        k, _m = plan_for(s)
        if k and k != INTER:
            keys.add(k)
    reg = registry()
    return [{"family": reg["fonts"][k]["family"], "file": Path(reg["fonts"][k]["path"]).name, "licence": "SIL Open Font License 1.1",
             "source": reg["sources"][reg["fonts"][k]["source"]]["repo"]} for k in sorted(keys)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("fetch", "status"))
    ap.add_argument("keys", nargs="?", default="")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print(json.dumps(status(), indent=2))
        return gc.EXIT_OK
    res = fetch([k for k in a.keys.split(",") if k], refresh=a.refresh)
    print(json.dumps(res, indent=2))
    return gc.EXIT_OK if all(r["ok"] for r in res) else gc.EXIT_SERVICE


if __name__ == "__main__":
    sys.exit(main())
