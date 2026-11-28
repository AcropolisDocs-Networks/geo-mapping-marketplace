#!/usr/bin/env python3
"""animate: an animated GIF and an MP4 from the pages of an atlas-by-time run (gap P2-2, M15).

    animate.py ATLAS_DIR --out DIR [--sizes square,slide] [--formats gif,mp4] [--hold 1.0] [--last-hold 3.0]
    map_export.py --animate ATLAS_DIR --out DIR [...]          (the same, from the export command)

ATLAS_DIR is a deliverable folder of the map-atlas-run pipeline made with `by: time` (it holds `atlas.json` and `specs/`, one MapSpec per period
in time order). Every period is drawn again at each size as its own map, never scaled from the print page:

    square   the square-social template at 108 dpi: 1080 x 1080 px (LinkedIn, Instagram)
    portrait the portrait-social template at 135 dpi: 1080 x 1350 px (4:5, for a tall subject; 1.2.0, asked for only)
    slide    the 16x9-slide template at 144 dpi: 1920 x 1080 px (a deck, a screen)

Each frame holds `hold` seconds, the last `last_hold` seconds; a thin progress bar in two brand neutrals runs along the inside of the map
frame's bottom edge (the period's share of the run; 5.7.0: inside the map's border, never under the credits). The GIF is written with Pillow (an adaptive 256-colour palette per frame). The MP4 needs ffmpeg, a SOFT dependency
found through imageio-ffmpeg (pip install imageio-ffmpeg) or on the PATH: H.264, `-preset veryfast`, yuv420p, even dimensions, 25 fps,
+faststart. Without ffmpeg the GIF is still written and the report says how to get the MP4. Nothing reaches the network.

5.8.0 (Day 27): `--timeline T.yaml` with a `chart:` draws the timeline's chart on every period's frame, inside the map frame and clear of the
legend, locator, title and logo (the plugin's own chart panel, heat_anim.paint_chart). Its rows follow the periods: by label when the atlas
names its periods, else in order. A chart with `source: table.csv, label: year, bar: col, baseline: first, reveal: true` shows each period's
change from the first, bar by bar. Only the chart is drawn on an atlas animation; a timeline's story lines and counters are noted as left out.

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


def progress_bar(im, i: int, n: int, tokens: dict, px: int, fill: bool = True, rect=None):
    """A thin bar: the track in the neutral ramp's second step, the filled share (period i of n) in its last. `fill=False` draws the track
    only (the MP4's fill is drawn by ffmpeg and moves smoothly through each hold, 1.0.0). 5.7.0 (a map is everything inside its border): with
    `rect` (the map frame in pixels, x0 y0 x1 y1) the bar runs along the inside of the frame's bottom edge, never under the credits; without
    it, along the picture's bottom edge (a bare frame)."""
    from PIL import ImageDraw
    w, h = im.size
    x0, x1, y1 = (0, w, h) if rect is None else (round(rect[0]), round(rect[2]), round(rect[3]))
    d = ImageDraw.Draw(im)
    ramp = tokens["neutral_ramp"]
    d.rectangle([x0, y1 - px, x1, y1], fill=_rgb(ramp[1]))
    if fill:
        d.rectangle([x0, y1 - px, x0 + round((x1 - x0) * (i + 1) / n), y1], fill=_rgb(ramp[-1]))
    return im


def smooth_bar_filter(w: int, h: int, px: int, n: int, hold: float, last_hold: float, ink: str, rect=None) -> str:
    """1.0.0 (TODO M15): the ffmpeg filter graph that slides the bar's fill in through each hold: period k of n fills from k/n to (k+1)/n
    over its own hold (the last over last_hold), so the bar reaches the end as the video does."""
    t1 = hold * (n - 1)
    frac = f"if(lt(t\\,{t1:g})\\,t/{hold * n:g}\\,({n - 1}+(t-{t1:g})/{last_hold:g})/{n})"
    x0, x1, y1 = (0, w, h) if rect is None else (round(rect[0]), round(rect[2]), round(rect[3]))
    bw = x1 - x0                                     # 5.7.0: the bar's own strip inside the map frame; the fill slides in within it
    ink_ = ink.lstrip("#")
    return (f"[0:v]fps={FPS},split[pic][cut];[cut]crop={bw}:{px}:{x0}:{y1 - px}[band];color=c=0x{ink_}:s={bw}x{px}:r={FPS}[bar];"
            f"[band][bar]overlay=x='-{bw}+{bw}*min(1\\,{frac})':y=0:eval=frame:shortest=1[filled];"
            f"[pic][filled]overlay=x={x0}:y={y1 - px}:shortest=1,format=yuv420p")


def render_frames(specs: list[Path], size: str, work: Path, *, brand: str | None = None, collect: dict | None = None,
                  frame_out: dict | None = None) -> tuple[list, dict, list[dict]]:
    """Each period's MapSpec drawn at one animation size. -> (frames as RGB images, the plan's tokens, findings). `collect` (1.3.0) gathers each
    frame's provenance records and the text it shows, for the video's terms register. 1.11.0: the frames share one ground and one read of
    every library layer (map_plan.frame_session), since only the data changes from period to period."""
    import map_plan as mp
    with mp.frame_session() as sess:
        sess["frames"] = True                      # 5.8.0: a frame's PNG is read back into the video, not kept: no slow optimize pass
        return _render_frames(specs, size, work, brand, collect, frame_out)


def _frame_spec(sp: Path, size: str, brand: str | None) -> dict:
    """A period's MapSpec as the frame draws it: the size's template and dpi; the brand the run names, if any, over the spec's own."""
    cfg = SIZES[size]
    spec = gc.load_mapspec(sp)
    spec["template"] = cfg["template"]
    spec["export"] = {**(spec.get("export") or {}), "dpi": cfg["dpi"]}
    if brand:
        spec.pop("brand", None)
    return spec


def _render_one(k: int, sp: Path, size: str, work: Path, brand: str | None) -> tuple[object, dict]:
    """One frame: build the period's map at the size into `work`. -> (the frame as an RGB image, what the video needs from its build)."""
    import build_map as bm
    import map_plan as mp
    import yaml
    cfg = SIZES[size]
    spec = _frame_spec(sp, size, brand)
    work.mkdir(parents=True, exist_ok=True)
    tmp = work / f"{size}-{k:03d}.yaml"
    tmp.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True), encoding="utf-8")
    try:
        res = bm.build(str(tmp), str(work / f"{size}-{k:03d}"), ["png"], brand=brand or spec.get("brand"), keep=True)
    except mp.PlanError as ex:
        raise AnimateError(f"frame {k + 1} ({sp.name}) cannot be built: {str(ex).splitlines()[0]}") from None
    obj = res.pop("_objects")
    tokens = obj["plan"].tokens
    info = {"tokens": tokens, "findings": [{**f, "message": f"[frame {k + 1} {size}] {f['message']}"} for f in res["findings"]
                                           if f["severity"] in ("BLOCK", "WARN")]}
    win = (res.get("report") or {}).get("map_window") or {}
    if win.get("rect_mm"):
        ppm = float(cfg["dpi"]) / 25.4                 # 5.7.0: the map frame in the frame's pixels, for the progress bar inside it
        info["frame_out"] = {"rect_px": [v * ppm for v in win["rect_mm"]], "report": res.get("report") or {}, "ppm": ppm}
    import terms_register as tr
    info["records"] = obj.get("records") or []
    info["shown"] = tr.svg_shown(obj["rendered"].svg)
    png = Path(next(f for f in res["files"] if f.endswith(".png"))).read_bytes()
    return fit_frame(png, *cfg["px"], _rgb(tokens["background"])), info


def _render_frames(specs: list[Path], size: str, work: Path, brand: str | None, collect: dict | None,
                   frame_out: dict | None = None) -> tuple[list, dict, list[dict]]:
    frames, findings, tokens = [], [], None
    for k, sp in enumerate(specs):
        im, info = _render_one(k, sp, size, work, brand)
        tokens = info["tokens"]
        if frame_out is not None and info.get("frame_out") and "rect_px" not in frame_out:
            frame_out.update(info["frame_out"])            # 5.8.0: the elements' boxes, which a timeline chart keeps clear of
        if collect is not None:
            collect.setdefault("records", []).extend(info["records"])
            collect.setdefault("shown", []).append(info["shown"])
        findings += info["findings"]
        frames.append(im)
    return frames, tokens, findings


# ---------------------------------------------------------------------------------------------------------------- kept frames (5.9.0)
FRAMES_DIR = ".frames"
_CODE_ID: str | None = None


def code_id() -> str:
    """What drew a frame: the plugin's version and a digest of its scripts and lints, so a frame drawn by another plugin is drawn again."""
    global _CODE_ID
    if _CODE_ID is None:
        import hashlib
        h = hashlib.blake2b(digest_size=12)
        root = HERE.parent
        for d in (HERE, root / "lints"):
            for f in sorted(d.glob("*.py")):
                h.update(f.name.encode())
                h.update(f.read_bytes())
        for f in (root / "VERSION", root / "BUILD-MANIFEST.json", root.parent / "VERSION"):
            if f.is_file():
                h.update(f.read_bytes())
        _CODE_ID = h.hexdigest()
    return _CODE_ID


def _files_named(obj, bases: list[Path], out: dict) -> dict:
    """Every file a MapSpec names (absolute, or beside the spec or the working folder), with its size and time: a frame whose data file
    changed is drawn again even when the spec's text did not."""
    if isinstance(obj, dict):
        for v in obj.values():
            _files_named(v, bases, out)
    elif isinstance(obj, list):
        for v in obj:
            _files_named(v, bases, out)
    elif isinstance(obj, str) and 0 < len(obj) < 400 and ("/" in obj or "\\" in obj or "." in obj) and "\n" not in obj:
        p = Path(obj)
        for c in ([p] if p.is_absolute() else [b / p for b in bases]):
            try:
                if c.is_file():
                    st = c.stat()
                    out[str(c.resolve())] = [st.st_size, st.st_mtime_ns]
                    break
            except OSError:
                continue
    return out


def frame_key(sp: Path, size: str, brand: str | None) -> str:
    """The key a kept frame is filed under: the frame's MapSpec as drawn, the size, the brand, the files it names and the plugin."""
    import hashlib
    spec = _frame_spec(sp, size, brand)
    files = _files_named(spec, [sp.parent, Path.cwd()], {})
    blob = json.dumps({"spec": spec, "size": size, "brand": brand, "files": files, "code": code_id()}, sort_keys=True, default=str)
    return hashlib.blake2b(blob.encode("utf-8"), digest_size=16).hexdigest()


def _kept(fdir: Path, k: int, key: str):
    """(image, info) of frame k when it is kept under the same key, else None."""
    side, png = fdir / f"{k:03d}.json", fdir / f"{k:03d}.png"
    try:
        info = json.loads(side.read_text(encoding="utf-8"))
        if info.get("key") != key or not png.is_file():
            return None
        from PIL import Image
        with Image.open(png) as im:
            return im.convert("RGB"), info
    except (OSError, ValueError):
        return None


def _keep(fdir: Path, k: int, key: str, im, info: dict, attention=None) -> dict:
    """File frame k: the picture first, then its sidecar (written last, so a frame cut off half way is drawn again)."""
    fdir.mkdir(parents=True, exist_ok=True)
    tmp = fdir / f"{k:03d}.png.part"
    im.save(tmp, format="PNG", compress_level=1)
    tmp.replace(fdir / f"{k:03d}.png")
    text = json.dumps({**info, "key": key, "frame": k, "attention": attention}, default=str)
    side = fdir / f"{k:03d}.json"
    side.with_suffix(".part").write_text(text, encoding="utf-8")
    side.with_suffix(".part").replace(side)
    return json.loads(text)                          # what a later run reads back, so a drawn frame and a kept one give the same report


def _frames_job(job: dict) -> list[int]:
    """A worker's share of the frames (5.9.0, --jobs): one frame session for its frames, seeded with the first frame's attention decision so
    every frame is drawn as a single run would draw it. Each frame is filed as soon as it is drawn."""
    import map_plan as mp
    done = []
    fdir = Path(job["fdir"])
    with mp.frame_session() as sess:
        sess["frames"] = True
        if job.get("attention") is not None:
            sess["attention"] = job["attention"]
        with tempfile.TemporaryDirectory() as td:
            for k in job["frames"]:
                im, info = _render_one(k, Path(job["specs"][k]), job["size"], Path(td), job["brand"])
                _keep(fdir, k, job["keys"][k], im, info, job.get("attention"))
                done.append(k)
    return done


def auto_jobs(n: int) -> int:
    """Worker processes for n frames to draw: one per core but one, at most four (each holds a map build), one for fewer than four frames."""
    import os
    return 1 if n < 4 else max(1, min(4, (os.cpu_count() or 2) - 1, n // 2))


def kept_frames(specs: list[Path], size: str, fdir: Path, *, brand: str | None = None, jobs=1, fresh: bool = False,
                progress=None) -> tuple[list, list[dict], dict]:
    """5.9.0: every period's frame at one size, kept in `fdir` (NNN.png and its NNN.json). A frame kept under the same key (its MapSpec as
    drawn, the size, the brand, the files it names, the plugin) is read back, not drawn; an animation cut off part way picks up where it
    stopped. `jobs` > 1 (or "auto") draws the frames left after the first in worker processes. -> (frames, infos, counts)."""
    import map_plan as mp
    fdir.mkdir(parents=True, exist_ok=True)
    keys = [frame_key(sp, size, brand) for sp in specs]
    have: dict[int, tuple] = {}
    if not fresh:
        for k, key in enumerate(keys):
            got = _kept(fdir, k, key)
            if got:
                have[k] = got
    todo = [k for k in range(len(specs)) if k not in have]
    n_jobs = auto_jobs(len(todo)) if jobs in ("auto", None, 0) else max(1, int(jobs))
    counts = {"folder": str(fdir), "reused": len(have), "drawn": len(todo), "jobs": 1}
    say = progress or (lambda _m: None)
    attention = (have[0][1].get("attention") if 0 in have else None)
    with mp.frame_session() as sess:
        sess["frames"] = True
        if attention is not None:
            sess["attention"] = attention
        with tempfile.TemporaryDirectory() as td:
            if todo and (0 not in have or n_jobs == 1):
                serial = todo if n_jobs == 1 else todo[:1]       # the first frame decides the attention boost for every frame
                for k in serial:
                    im, info = _render_one(k, specs[k], size, Path(td), brand)
                    attention = sess.get("attention") if k == 0 or attention is None else attention
                    have[k] = (im, _keep(fdir, k, keys[k], im, info, attention))
                    say(f"{size}: frame {k + 1} of {len(specs)} drawn")
                todo = [k for k in todo if k not in have]
    if todo:
        n_jobs = min(n_jobs, len(todo))
        counts["jobs"] = n_jobs
        import multiprocessing as mpr
        from concurrent.futures import ProcessPoolExecutor, as_completed
        base = {"specs": [str(s) for s in specs], "size": size, "brand": brand, "keys": keys, "fdir": str(fdir), "attention": attention}
        broke = None
        try:
            with ProcessPoolExecutor(max_workers=n_jobs, mp_context=mpr.get_context("spawn")) as ex:
                futs = [ex.submit(_frames_job, {**base, "frames": todo[i::n_jobs]}) for i in range(n_jobs)]
                for fu in as_completed(futs):
                    fu.result()
                    say(f"{size}: a worker finished")
        except AnimateError:
            raise
        except Exception as e:  # noqa: BLE001 - workers that could not start or went down: what they filed stays, the rest is drawn here
            broke = f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
        left = []
        for k in todo:
            got = _kept(fdir, k, keys[k])
            if got:
                have[k] = got
            else:
                left.append(k)
        if left:
            counts["note"] = (f"the frame workers stopped ({broke or 'a frame was not filed'}); {len(left)} frame(s) were drawn in this process")
            say(f"{size}: {counts['note']}")
            with mp.frame_session() as sess:
                sess["frames"] = True
                if attention is not None:
                    sess["attention"] = attention
                with tempfile.TemporaryDirectory() as td:
                    for k in left:
                        im, info = _render_one(k, specs[k], size, Path(td), brand)
                        have[k] = (im, _keep(fdir, k, keys[k], im, info, attention))
    frames = [have[k][0] for k in range(len(specs))]
    infos = [have[k][1] for k in range(len(specs))]
    for f in fdir.glob("*.png"):                       # frames past the end of a shorter run are not kept
        if f.stem.isdigit() and int(f.stem) >= len(specs):
            f.unlink(missing_ok=True)
            (fdir / f"{f.stem}.json").unlink(missing_ok=True)
    return frames, infos, counts


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
        (Path(td) / "frames.txt").write_text("\n".join(lst) + "\n", encoding="utf-8")
        total = hold * (len(frames) - 1) + last_hold
        # -t: the closing entry's length differs by ffmpeg version (6.1 gives it none, 9.0 the last hold again: 8 s for 5), so the
        # output is cut at the holds' sum; the closing entry keeps older versions from dropping the last hold
        vf = (["-filter_complex", smooth_bar_filter(w, h, bar["px"], len(frames), hold, last_hold, bar["ink"], rect=bar.get("rect"))] if bar
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
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    if meta.get("by") != "time":
        raise AnimateError("this atlas is one map per value, not per period; an animation needs an atlas made with by: time")
    specs = sorted((atlas_dir / "specs").glob("*.yaml"))
    if len(specs) < 2:
        raise AnimateError("an animation needs at least two periods (specs/ holds fewer)")
    return specs, meta


def load_timeline_chart(timeline, specs: list[Path], meta: dict) -> tuple[dict | None, list[int], list[str]]:
    """5.8.0 (Day 27): the timeline's chart for an atlas animation and, for each period, the row that is current. -> (chart, rows, notes)."""
    if not timeline:
        return None, [], []
    import timeline as tlm
    try:
        tl = tlm.load(timeline)
    except tlm.TimelineError as ex:
        raise AnimateError(f"the timeline: {ex}") from None
    ch = tl.get("chart")
    if not ch:
        raise AnimateError("an atlas animation draws a timeline's chart: give the timeline a chart: block")
    if ch.get("kind") == "profile":
        raise AnimateError("a profile chart runs along a flow; an atlas animation takes a bars chart")
    notes = []
    left_out = [k for k in ("story", "counters", "keyframes", "features") if tl.get(k)]
    if left_out:
        notes.append(f"the timeline's {', '.join(left_out)} are not drawn on an atlas animation (its chart is)")
    labels = [r["label"] for r in ch["rows"]]
    periods = [str(p) for p in (meta.get("periods") or [])]
    if periods and len(periods) == len(specs) and all(p in labels for p in periods):
        cur = [labels.index(p) for p in periods]
    elif len(labels) == len(specs):
        cur = list(range(len(specs)))
    else:
        raise AnimateError(f"the chart has {len(labels)} rows and the atlas {len(specs)} periods; name the periods in atlas.json (periods: [...]) "
                           "with the rows' labels, or give one row per period")
    for i, r in enumerate(ch["rows"]):
        r["start"] = float(i)                        # the row current at chart time i; each period's frame is painted at its row's time
    return ch, cur, notes


def paint_chart_frames(frames, chart: dict, cur: list[int], tokens: dict, fr: dict) -> tuple[list, list[dict]]:
    """5.8.0: the chart panel on each period's frame (the period's row current), kept inside the map frame and clear of its elements."""
    import events_anim as ev
    import flythrough as fl
    import heat_anim as ha
    font = str(fl._inter_ttf(600))
    rect = fr.get("rect_px")
    avoid = ev.frame_avoid(fr.get("report"), fr.get("ppm") or 1.0, frames[0].size, {})
    out, findings, outside = [], [], None
    for i, f in enumerate(frames):
        im, box = ha.paint_chart(f, float(cur[i]), chart, tokens, font_path=font, avoid=avoid, frame=rect)
        if rect and not ev.inside_frame(box, rect) and outside is None:
            outside = i + 1
        out.append(im)
    if outside:
        findings.append({"id": "MAP-CART-046", "severity": "WARN", "message": f"the chart leaves the map frame (frame {outside})", "detail": None})
    return out, findings


def animate(atlas_dir, out_dir, *, sizes=("square", "slide"), formats=("gif", "mp4"), hold: float = 1.0, last_hold: float = 3.0, brand: str | None = None,
            stem: str | None = None, timeline=None, jobs=1, fresh: bool = False, frames_dir=None, progress=None) -> dict:
    """Frames for every period at every size, then the GIF and the MP4 per size, each read back by the output lint. 5.8.0: `timeline` (a
    path, a YAML string or a dict) with a chart: the chart drawn on every frame. 5.9.0: the frames are kept in `frames_dir` (default
    OUT/.frames/<size>) and read back on the next run when nothing they are drawn from changed; `jobs` draws them in parallel, `fresh`
    draws every frame again."""
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
    chart, chart_cur, cnotes = load_timeline_chart(timeline, specs, meta)
    rep["notes"] += cnotes
    if chart:
        rep["chart"] = {"rows": len(chart["rows"]), "baseline": chart.get("baseline"), "reveal": chart.get("reveal")}
    seen: dict = {}
    fbase = Path(frames_dir) if frames_dir else out_dir / FRAMES_DIR
    rep["frames"] = {}
    for size in sizes:
        frames, infos, counts = kept_frames(specs, size, fbase / size, brand=brand, jobs=jobs, fresh=fresh, progress=progress)
        rep["frames"][size] = counts
        if counts.get("note"):
            rep["notes"].append(f"[{size}] {counts['note']}")
        tokens = infos[-1]["tokens"]
        fr_ = next((i["frame_out"] for i in infos if i.get("frame_out")), {})
        bar_rect = fr_.get("rect_px")
        for i in infos:
            seen.setdefault("records", []).extend(i.get("records") or [])
            seen.setdefault("shown", []).append(i.get("shown") or "")
            rep["findings"] += i.get("findings") or []
        if chart:
            frames, cfs = paint_chart_frames(frames, chart, chart_cur, tokens, fr_)
            rep["findings"] += [{**f_, "message": f"[{size}] {f_['message']}"} for f_ in cfs]
        raw = [f.copy() for f in frames]
        frames = [progress_bar(f, i, len(frames), tokens, BAR_PX[size], rect=bar_rect) for i, f in enumerate(frames)]
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
                tracks = [progress_bar(f.copy(), i, len(raw), tokens, BAR_PX[size], fill=False, rect=bar_rect) for i, f in enumerate(raw)]
                m = write_mp4(tracks, p, hold, last_hold, {"title": str(meta.get("title") or stem), "comment": comment,
                                                            "copyright": props.get("Author", ""), "artist": props.get("Author", "")},
                              bar={"px": BAR_PX[size], "ink": tokens["neutral_ramp"][-1], "rect": bar_rect})
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
    ap.add_argument("--timeline", help="5.8.0: a timeline with a chart: block, drawn on every period's frame")
    ap.add_argument("--jobs", default="auto", help="5.9.0: frames drawn at once (worker processes), or auto (one per core but one, at most 4)")
    ap.add_argument("--fresh", action="store_true", help="5.9.0: draw every frame again, not the kept ones")
    a = ap.parse_args(argv)
    try:
        r = animate(a.atlas, a.out, sizes=[s.strip() for s in a.sizes.split(",") if s.strip()], formats=[s.strip() for s in a.formats.split(",") if s.strip()],
                    hold=a.hold, last_hold=a.last_hold, brand=a.brand, timeline=a.timeline, jobs=a.jobs, fresh=a.fresh,
                    progress=lambda m: print(m, file=sys.stderr, flush=True))
    except AnimateError as ex:
        print(f"animate: {ex}", file=sys.stderr)
        return gc.EXIT_USAGE
    print(json.dumps(r, indent=2, default=str))
    return gc.EXIT_LINT if r["blocked"] else gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
