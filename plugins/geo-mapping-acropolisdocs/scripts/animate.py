#!/usr/bin/env python3
"""animate: an animated GIF and an MP4 from the pages of an atlas-by-time run (gap P2-2, M15).

    animate.py ATLAS_DIR --out DIR [--sizes square,slide] [--formats gif,mp4] [--hold 1.0] [--last-hold 3.0]
    map_export.py --animate ATLAS_DIR --out DIR [...]          (the same, from the export command)

ATLAS_DIR is a deliverable folder of the map-atlas-run pipeline made with `by: time` (it holds `atlas.json` and `specs/`, one MapSpec per period
in time order). Every period is drawn again at each size as its own map, never scaled from the print page:

    square   the square-social template at 108 dpi: 1080 x 1080 px (LinkedIn, Instagram)
    portrait the portrait-social template at 135 dpi: 1080 x 1350 px (4:5, for a tall subject; 1.2.0, asked for only)
    slide    the 16x9-slide template at 144 dpi: 1920 x 1080 px (a deck, a screen)

Each frame holds `hold` seconds, the last `last_hold` seconds; a thin progress bar in two brand neutrals runs along the bottom edge (the
period's share of the run). The GIF is written with Pillow (an adaptive 256-colour palette per frame). The MP4 needs ffmpeg, a SOFT dependency
found through imageio-ffmpeg (pip install imageio-ffmpeg) or on the PATH: H.264, `-preset veryfast`, yuv420p, even dimensions, 25 fps,
+faststart. Without ffmpeg the GIF is still written and the report says how to get the MP4. Nothing reaches the network.

The hidden accreditation goes into the GIF comment and the MP4 metadata (comment, copyright), never into a frame. output_lint.check_gif and
check_mp4 read both files back (MAP-OUT-013): frame count, size, timing, codec and pixel format.
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lints"))
import gmap_common as gc  # noqa: E402

SIZES = {"square": {"template": "square-social", "px": (1080, 1080), "dpi": 108.0},
         "portrait": {"template": "portrait-social", "px": (1080, 1350), "dpi": 135.0},      # 1.2.0: 4:5 for tall subjects
         "slide": {"template": "16x9-slide", "px": (1920, 1080), "dpi": 144.0}}
FPS = 25
BAR_PX = {"square": 6, "portrait": 6, "slide": 6}
SECONDS_PER_RENDER = 6


class AnimateError(RuntimeError):
    pass


def ffmpeg_exe() -> str | None:
    """imageio-ffmpeg's bundled binary first, then a system ffmpeg; None when neither is there."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return exe
    except Exception:  # noqa: BLE001 - not installed, or its binary is missing
        pass
    return shutil.which("ffmpeg")


FFMPEG_MISSING = "the MP4 needs ffmpeg: pip install imageio-ffmpeg (or install ffmpeg on the PATH); the GIF was written without it"


def cost(n_periods: int, sizes: list[str], formats: list[str]) -> dict:
    """What an animation costs: one render per period and size (the frames), the files it writes and about how long it takes."""
    frames = n_periods * len(sizes)
    return {"frames": frames, "renders": frames, "files_written": len(sizes) * len(formats), "est_seconds": frames * SECONDS_PER_RENDER + 10 * len(sizes),
            "note": f"animation: {frames} frame render(s) ({n_periods} period(s) x {len(sizes)} size(s): {', '.join(sizes)}), "
                    f"{', '.join(f.upper() for f in formats)}" + ("" if "mp4" not in formats or ffmpeg_exe() else "; ffmpeg is missing, so no MP4")}


def check_sizes(sizes: list[str]) -> list[str]:
    bad = [s for s in sizes if s not in SIZES]
    if bad:
        raise AnimateError(f"unknown animation size(s) {', '.join(bad)}; use square (1080 x 1080) and/or slide (1920 x 1080)")
    return sizes


def fit_frame(png: bytes, w: int, h: int, ground: tuple[int, int, int]):
    """The rendered page at exactly w x h pixels (a page rasterised at a fractional size can come out a pixel short): pad or crop, never scale."""
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGB")
    if im.size == (w, h):
        return im
    out = Image.new("RGB", (w, h), ground)
    out.paste(im.crop((0, 0, min(w, im.width), min(h, im.height))), (0, 0))
    return out


def _rgb(hexs: str) -> tuple[int, int, int]:
    h = hexs.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def progress_bar(im, i: int, n: int, tokens: dict, px: int, fill: bool = True):
    """A thin bar along the bottom edge: the track in the neutral ramp's second step, the filled share (period i of n) in its last. `fill=False`
    draws the track only (the MP4's fill is drawn by ffmpeg and moves smoothly through each hold, 1.0.0)."""
    from PIL import ImageDraw
    w, h = im.size
    d = ImageDraw.Draw(im)
    ramp = tokens["neutral_ramp"]
    d.rectangle([0, h - px, w, h], fill=_rgb(ramp[1]))
    if fill:
        d.rectangle([0, h - px, round(w * (i + 1) / n), h], fill=_rgb(ramp[-1]))
    return im


def smooth_bar_filter(w: int, h: int, px: int, n: int, hold: float, last_hold: float, ink: str) -> str:
    """1.0.0 (TODO M15): the ffmpeg filter graph that slides the bar's fill in through each hold: period k of n fills from k/n to (k+1)/n
    over its own hold (the last over last_hold), so the bar reaches the end as the video does."""
    t1 = hold * (n - 1)
    frac = f"if(lt(t\\,{t1:g})\\,t/{hold * n:g}\\,({n - 1}+(t-{t1:g})/{last_hold:g})/{n})"
    return (f"[0:v]fps={FPS}[base];color=c=0x{ink.lstrip('#')}:s={w}x{px}:r={FPS}[bar];"
            f"[base][bar]overlay=x='-{w}+{w}*min(1\\,{frac})':y={h - px}:eval=frame:shortest=1,format=yuv420p")


def render_frames(specs: list[Path], size: str, work: Path, *, brand: str | None = None, collect: dict | None = None) -> tuple[list, dict, list[dict]]:
    """Each period's MapSpec drawn at one animation size. -> (frames as RGB images, the plan's tokens, findings). `collect` (1.3.0) gathers each
    frame's provenance records and the text it shows, for the video's terms register."""
    import build_map as bm
    import map_plan as mp
    cfg = SIZES[size]
    frames, findings, tokens = [], [], None
    work.mkdir(parents=True, exist_ok=True)
    for k, sp in enumerate(specs):
        spec = gc.load_mapspec(sp)
        spec["template"] = cfg["template"]
        spec["export"] = {**(spec.get("export") or {}), "dpi": cfg["dpi"]}
        spec.pop("brand", None) if brand else None
        tmp = work / f"{size}-{k:03d}.yaml"
        import yaml
        tmp.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))
        try:
            res = bm.build(str(tmp), str(work / f"{size}-{k:03d}"), ["png"], brand=brand or spec.get("brand"), keep=True)
        except mp.PlanError as ex:
            raise AnimateError(f"frame {k + 1} ({sp.name}) cannot be built: {str(ex).splitlines()[0]}") from None
        obj = res.pop("_objects")
        tokens = obj["plan"].tokens
        if collect is not None:
            import terms_register as tr
            collect.setdefault("records", []).extend(obj.get("records") or [])
            collect.setdefault("shown", []).append(tr.svg_shown(obj["rendered"].svg))
        findings += [{**f, "message": f"[frame {k + 1} {size}] {f['message']}"} for f in res["findings"] if f["severity"] in ("BLOCK", "WARN")]
        png = Path(next(f for f in res["files"] if f.endswith(".png"))).read_bytes()
        frames.append(fit_frame(png, *cfg["px"], _rgb(tokens["background"])))
    return frames, tokens, findings


def write_gif(frames, path: Path, hold: float, last_hold: float, comment: str = "") -> dict:
    from PIL import Image
    pal = [f.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in frames]
    dur = [round(hold * 1000)] * (len(pal) - 1) + [round(last_hold * 1000)]
    pal[0].save(path, save_all=True, append_images=pal[1:], duration=dur, loop=0, optimize=False, disposal=1, comment=comment.encode("utf-8"))
    return {"path": str(path), "frames": len(pal), "durations_ms": dur, "bytes": path.stat().st_size}


def write_mp4(frames, path: Path, hold: float, last_hold: float, meta: dict, bar: dict | None = None) -> dict:
    exe = ffmpeg_exe()
    if not exe:
        raise AnimateError(FFMPEG_MISSING)
    w, h = frames[0].size
    if w % 2 or h % 2:
        raise AnimateError(f"H.264 with yuv420p needs even dimensions; the frames are {w} x {h}")
    with tempfile.TemporaryDirectory() as td:
        lst = ["ffconcat version 1.0"]
        for k, f in enumerate(frames):
            p = Path(td) / f"f{k:04d}.png"
            f.save(p)
            lst += [f"file '{p.name}'", f"duration {last_hold if k == len(frames) - 1 else hold:g}"]
        lst.append(f"file 'f{len(frames) - 1:04d}.png'")                 # the concat demuxer drops the last duration without a closing entry
        (Path(td) / "frames.txt").write_text("\n".join(lst) + "\n")
        total = hold * (len(frames) - 1) + last_hold
        # -t: the closing entry's length differs by ffmpeg version (6.1 gives it none, 9.0 the last hold again: 8 s for 5), so the
        # output is cut at the holds' sum; the closing entry keeps older versions from dropping the last hold
        vf = (["-filter_complex", smooth_bar_filter(w, h, bar["px"], len(frames), hold, last_hold, bar["ink"])] if bar
              else ["-vf", f"fps={FPS},format=yuv420p"])
        cmd = [exe, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(Path(td) / "frames.txt"), *vf,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-t", f"{total:.3f}"]
        for k, v in meta.items():
            cmd += ["-metadata", f"{k}={v}"]
        cmd.append(str(path))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not path.is_file():
            raise AnimateError(f"ffmpeg failed: {r.stderr.strip()[:300]}")
    return {"path": str(path), "frames_in": len(frames), "fps": FPS, "seconds": hold * (len(frames) - 1) + last_hold, "bytes": path.stat().st_size}


def specs_of(atlas_dir: Path) -> tuple[list[Path], dict]:
    meta_p = atlas_dir / "atlas.json"
    if not meta_p.is_file():
        raise AnimateError(f"{atlas_dir} has no atlas.json; give the folder the map-atlas-run pipeline wrote (by: time)")
    meta = json.loads(meta_p.read_text())
    if meta.get("by") != "time":
        raise AnimateError("this atlas is one map per value, not per period; an animation needs an atlas made with by: time")
    specs = sorted((atlas_dir / "specs").glob("*.yaml"))
    if len(specs) < 2:
        raise AnimateError("an animation needs at least two periods (specs/ holds fewer)")
    return specs, meta


def animate(atlas_dir, out_dir, *, sizes=("square", "slide"), formats=("gif", "mp4"), hold: float = 1.0, last_hold: float = 3.0, brand: str | None = None,
            stem: str | None = None) -> dict:
    """Frames for every period at every size, then the GIF and the MP4 per size, each read back by the output lint."""
    import map_brand
    import output_lint
    atlas_dir, out_dir = Path(atlas_dir), Path(out_dir)
    sizes = check_sizes(list(sizes))
    formats = [f for f in formats if f in ("gif", "mp4")] or ["gif"]
    if not (0.2 <= hold <= 10) or not (hold <= last_hold <= 20):
        raise AnimateError("hold is 0.2 to 10 seconds and last_hold at least hold and at most 20")
    specs, meta = specs_of(atlas_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or str(meta.get("base_id") or atlas_dir.name.replace("-atlas", ""))
    rep = {"periods": len(specs), "sizes": sizes, "files": [], "findings": [], "notes": []}
    seen: dict = {}
    with tempfile.TemporaryDirectory() as td:
        for size in sizes:
            frames, tokens, fs = render_frames(specs, size, Path(td) / size, brand=brand, collect=seen)
            rep["findings"] += fs
            raw = [f.copy() for f in frames]
            frames = [progress_bar(f, i, len(frames), tokens, BAR_PX[size]) for i, f in enumerate(frames)]
            slug_ = brand or gc.load_mapspec(specs[0]).get("brand")
            props = map_brand.props_for(slug_, gc.build_manifest(), "png")["tEXt"] if slug_ else {}
            comment = "; ".join(f"{k}: {v}" for k, v in props.items() if k in ("Author", "Accreditation", "BrandPack", "Software"))
            w, h = SIZES[size]["px"]
            if "gif" in formats:
                p = out_dir / f"{stem}_{size}_{w}x{h}.gif"
                g = write_gif(frames, p, hold, last_hold, comment)
                rep["files"].append(g)
                rep["findings"] += output_lint.check_gif(str(p), (w, h), len(frames), hold, last_hold)
            if "mp4" in formats:
                p = out_dir / f"{stem}_{size}_{w}x{h}.mp4"
                try:
                    tracks = [progress_bar(f.copy(), i, len(raw), tokens, BAR_PX[size], fill=False) for i, f in enumerate(raw)]
                    m = write_mp4(tracks, p, hold, last_hold, {"title": str(meta.get("title") or stem), "comment": comment,
                                                                "copyright": props.get("Author", ""), "artist": props.get("Author", "")},
                                  bar={"px": BAR_PX[size], "ink": tokens["neutral_ramp"][-1]})
                    rep["files"].append(m)
                    rep["findings"] += output_lint.check_mp4(str(p), (w, h), hold * (len(frames) - 1) + last_hold)
                except AnimateError as ex:
                    if FFMPEG_MISSING in str(ex):
                        rep["notes"].append(str(ex))
                        rep["findings"].append({"id": "MAP-OUT-013", "severity": "WARN", "message": str(ex), "detail": None})
                    else:
                        raise
    if rep["files"] and seen.get("records"):            # 1.3.0 (R1-TERMS): the video's credits are the frames' own source notes
        import terms_register as tr
        tdoc, tfs, tfiles = tr.for_deliverable(out_dir, seen["records"], deliverable="video", stem=stem, outputs={"video": " ".join(seen["shown"])},
                                               title=str(meta.get("title") or stem))
        rep["findings"] += tfs
        rep["terms"] = {"files": [str(f) for f in tfiles], "summary": tdoc["summary"]}
    rep["blocked"] = any(f["severity"] == "BLOCK" for f in rep["findings"])
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("atlas")
    ap.add_argument("--out", required=True)
    ap.add_argument("--sizes", default="square,slide")
    ap.add_argument("--formats", default="gif,mp4")
    ap.add_argument("--hold", type=float, default=1.0)
    ap.add_argument("--last-hold", type=float, default=3.0)
    ap.add_argument("--brand")
    a = ap.parse_args(argv)
    try:
        r = animate(a.atlas, a.out, sizes=[s.strip() for s in a.sizes.split(",") if s.strip()], formats=[s.strip() for s in a.formats.split(",") if s.strip()],
                    hold=a.hold, last_hold=a.last_hold, brand=a.brand)
    except AnimateError as ex:
        print(f"animate: {ex}", file=sys.stderr)
        return gc.EXIT_USAGE
    print(json.dumps(r, indent=2, default=str))
    return gc.EXIT_LINT if r["blocked"] else gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
