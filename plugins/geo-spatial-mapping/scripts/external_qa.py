#!/usr/bin/env python3
"""external_qa: the plugin's checks and paper trail for a still or a video made outside its pipeline (1.16.0, Day 14 issue 13).

    map_export.py --external FILE --spec MAP.yaml [--credits-shown "TEXT"] [--out DIR] [--brand SLUG]

Day 14's video was a custom HTML render captured in Playwright, so no brand lint, provenance or QA ran on it. This gives such a file what a
pipeline build gets, from the MapSpec that describes its data (the layers' sources, licences and dates):

- the file read back: a PNG or JPEG (size, readable), or an MP4 (H.264, yuv420p, even dimensions: MAP-OUT-013)
- the credits its sources require (provenance.required_credit_lines), each looked for in the credits it shows (`--credits-shown`, the text
  in its credits band): a required credit that is missing is MAP-OUT-028 (BLOCK); without --credits-shown the lines are listed to check by eye
- its colours against the brand's palette: a colour over 3 percent of the picture that is neither a brand colour (within delta E 12) nor a
  neutral is MAP-OUT-029 (WARN), the colours named
- a provenance sidecar beside the file (<file>.provenance.json, the same writer as a build) and the hidden accreditation in the file's own
  metadata (PNG text chunks; the MP4's metadata, copied without re-encoding)
- the checks a person makes by eye (the logo bottom left, title and subtitle text, the illustrative note) listed in `confirm`
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import gmap_common as gc  # noqa: E402

PALETTE_SHARE = 0.03
PALETTE_DELTA_E = 12.0
NEUTRAL_CHROMA = 12.0


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def credits_missing(required: list[str], shown: str) -> list[str]:
    """The required credit lines whose words are not in the credits shown (case, punctuation and the order of lines ignored)."""
    have = _norm(shown)
    out = []
    for line in required:
        core = _norm(re.sub(r"^(sources?|data|map data|contains)\s*:\s*", "", line, flags=re.I))
        if core and core not in have:
            out.append(line)
    return out


def _lab(rgb):
    import numpy as np
    c = np.asarray(rgb, float) / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = c @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def off_palette(img, palette: list[str], *, share: float = PALETTE_SHARE) -> list[dict]:
    """The colours covering more than `share` of the picture that are neither near a brand colour nor a neutral: [{hex, share, delta_e}]."""
    import numpy as np
    im = img.convert("RGB").resize((min(320, img.width), max(1, round(img.height * min(320, img.width) / img.width))))
    q = im.quantize(colors=24, method=2)
    pal = q.getpalette()[:72]
    counts = sorted(q.getcolors(), reverse=True)
    total = sum(c for c, _i in counts)
    brand = _lab([[int(h[i:i + 2], 16) for i in (1, 3, 5)] for h in palette if isinstance(h, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", h)])
    out = []
    for c, idx in counts:
        if c / total < share:
            continue
        rgb = pal[idx * 3: idx * 3 + 3]
        lab = _lab([rgb])[0]
        if (lab[1] ** 2 + lab[2] ** 2) ** 0.5 < NEUTRAL_CHROMA:
            continue
        d = float(np.min(np.linalg.norm(brand - lab, axis=1))) if len(brand) else 99.0
        if d > PALETTE_DELTA_E:
            out.append({"hex": "#{:02x}{:02x}{:02x}".format(*rgb), "share": round(c / total, 3), "delta_e": round(d, 1)})
    return out


def _palette(tokens: dict) -> list[str]:
    out = []

    def walk(v):
        if isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(tokens)
    return sorted(set(out))


def _first_frame(path: Path):
    import imageio_ffmpeg
    from PIL import Image
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    r = subprocess.run([exe, "-loglevel", "error", "-i", str(path), "-vf", "select=eq(n\\,0)", "-vframes", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                       capture_output=True)
    import io
    return Image.open(io.BytesIO(r.stdout)) if r.stdout else None


def run(file, spec, *, credits_shown: str | None = None, out_dir=None, brand: str | None = None) -> dict:
    gc.ledger_start_lazy("external_qa.run")
    import map_brand
    import map_plan as mp
    import output_lint as ol
    import provenance as pv
    f = Path(file)
    if not f.is_file():
        raise FileNotFoundError(f"{f} not found")
    plan = mp.prepare(str(spec), brand=brand) if brand else mp.prepare(str(spec))
    findings: list[dict] = []
    kind = f.suffix.lower().lstrip(".")
    img = None
    if kind == "mp4":
        info = ol.mp4_info(str(f))
        findings += ol.check_mp4(str(f), tuple(info["size"]), info["seconds"])
        try:
            img = _first_frame(f)
        except Exception:  # noqa: BLE001
            img = None
    elif kind in ("png", "jpg", "jpeg"):
        from PIL import Image
        img = Image.open(f)
        img.load()
    else:
        raise ValueError("external QA reads a PNG, JPEG or MP4")
    required = pv.required_credit_lines(plan)
    if credits_shown is not None:
        for line in credits_missing(required, credits_shown):
            findings.append(finding("MAP-OUT-028", "BLOCK", f"the required credit '{line}' is not in the credits the file shows", line))
    if img is not None:
        off = off_palette(img, _palette(plan.tokens))
        if off:
            findings.append(finding("MAP-OUT-029", "WARN", "colours outside the brand palette cover part of the picture: "
                                    + ", ".join(f"{o['hex']} ({o['share']:.0%}, delta E {o['delta_e']:g})" for o in off[:5]), off))
    out = Path(out_dir) if out_dir else f.parent
    out.mkdir(parents=True, exist_ok=True)
    side = pv.write_sidecar(out / f"{f.name}.provenance.json", plan, pv.records(plan), None)
    manifest = gc.build_manifest()
    props = map_brand.props_for(plan.brand, manifest, "png")["tEXt"]
    stamped = None
    if kind == "png":
        from PIL import Image, PngImagePlugin
        info_ = PngImagePlugin.PngInfo()
        for k, v in props.items():
            info_.add_text(k, str(v))
        dst = out / f.name if out != f.parent else f
        Image.open(f).save(dst, pnginfo=info_)
        stamped = str(dst)
    elif kind == "mp4":
        import imageio_ffmpeg
        dst = out / (f.stem + "_qa.mp4")
        comment = "; ".join(f"{k}: {v}" for k, v in props.items())
        r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(f), "-c", "copy", "-metadata", f"comment={comment}",
                            "-metadata", f"artist={props.get('Author', '')}", "-movflags", "+faststart", str(dst)], capture_output=True, text=True)
        stamped = str(dst) if r.returncode == 0 else None
        if stamped is None:
            shutil.copyfile(f, dst)
    confirm = ["the brand logo bottom left, the brand's own file, not recoloured", "the title in Title Case and the subtitle line",
               "the credits band readable at the size it is posted"]
    if any("illustrative" in (str((l.spec or {}).get("note") or "") + str((l.spec or {}).get("provenance") or "")).lower() for l in plan.layers):
        confirm.append("the 'illustrative' note on screen for the layers that are not surveyed")
    return {"ok": not any(x["severity"] == "BLOCK" for x in findings), "file": str(f), "findings": findings, "required_credits": required,
            "provenance": str(side), "stamped": stamped, "confirm": confirm}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("file")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--credits-shown")
    ap.add_argument("--out")
    ap.add_argument("--brand")
    a = ap.parse_args(argv)
    try:
        rep = run(a.file, a.spec, credits_shown=a.credits_shown, out_dir=a.out, brand=a.brand)
    except (FileNotFoundError, ValueError) as ex:
        print(f"external_qa: {ex}", file=sys.stderr)
        return gc.EXIT_USAGE
    print(json.dumps(rep, indent=2, default=str))
    return gc.EXIT_OK if rep["ok"] else gc.EXIT_LINT


if __name__ == "__main__":
    sys.exit(main())
