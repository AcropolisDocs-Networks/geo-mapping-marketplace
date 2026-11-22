#!/usr/bin/env python3
"""reveal: a layer that appears feature by feature in a map video (3.4.0, Day 21 Antakya issue 3).

    timeline:
      clock: [["2023-02-05", 0], ["2023-02-08", 6], ["2023-02-11", 22], ["2023-03-15", 40]]
      reveal: {layer: buildings, time_field: created, end_field: deleted, class_field: kind,
               colours: {before: "@brand.categorical.4", traced: "@brand.categorical.7", ai: "@brand.status.warning"},
               flash_s: 0.6, before: before}

The Day 21 video had 65,000 buildings to bring in. The timeline's `features:` route redraws every moving piece each frame (and rebuilt its
set of resting pieces per piece), so a city could not appear through it; render.py drew the buildings itself. This is that drawing, in the
plugin:

- the layer is drawn once into an id image at `supersample` times the frame (3 by default), each feature its own number, the later ones
  over the earlier;
- each frame looks the ids up in a colour table built from the times: transparent before a feature's time (or after its end), the flash
  colour fading into its class colour over `flash_s` seconds after it, its class colour after that; features of the class named `before`
  are there from the first frame;
- the picture is averaged down to the frame, so the edges are smooth, and composited over the base map; with `glow`, the flashing
  features get a soft halo.

The work per frame is one table lookup and one average, whatever the number of features. Times are a time column (dates or numbers) read
through the timeline's clock when it has one, else seconds of video. The flash is keyed in the legend as `flash_label` ("Just added"), so a
transient state is never read as a data class.
"""
from __future__ import annotations

import math

import numpy as np


class RevealError(ValueError):
    pass


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = str(h).lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def times_of(values, clock) -> np.ndarray:
    """Video seconds for each value: through the clock for dates and times, as given for numbers; NaN for a missing value."""
    import timeline as tlm
    out = np.full(len(values), np.nan)
    for i, v in enumerate(values):
        if v is None or (isinstance(v, float) and math.isnan(v)) or (isinstance(v, str) and not v.strip()):
            continue
        try:
            import pandas as pd
            if pd.isna(v):
                continue
        except (TypeError, ValueError, ImportError):
            pass
        if isinstance(v, (int, float, np.integer, np.floating)) and not clock:
            out[i] = float(v)
        else:
            out[i] = tlm.to_video(clock, v) if clock else float(v)
    return out


class Reveal:
    def __init__(self, gdf, cfg: dict, clock, to_px, size: tuple[int, int], tokens: dict, resolve_colour):
        from PIL import Image, ImageDraw
        if cfg["time_field"] not in gdf.columns:
            raise RevealError(f"reveal: time_field '{cfg['time_field']}' is not a column of '{cfg['layer']}'")
        for k in ("end_field", "class_field"):
            if cfg.get(k) and cfg[k] not in gdf.columns:
                raise RevealError(f"reveal: {k} '{cfg[k]}' is not a column of '{cfg['layer']}'")
        g = gdf.reset_index(drop=True)
        self.n = len(g)
        self.born = times_of(g[cfg["time_field"]].tolist(), clock)
        self.died = times_of(g[cfg["end_field"]].tolist(), clock) if cfg.get("end_field") else np.full(self.n, np.inf)
        self.died = np.where(np.isnan(self.died), np.inf, self.died)
        classes = [str(v) for v in g[cfg["class_field"]].tolist()] if cfg.get("class_field") else ["_"] * self.n
        before = cfg.get("before")
        if before is not None:
            self.born = np.where(np.array(classes) == str(before), -np.inf, self.born)
        self.born = np.where(np.isnan(self.born), np.inf, self.born)
        names = list(dict.fromkeys(classes))
        cols = cfg.get("colours") or {}
        default = tokens.get("emphasis")
        self.class_rgb = np.array([_hex_rgb(resolve_colour(cols.get(c)) if cols.get(c) else (default if len(names) == 1 else
                                                                                               (tokens.get("categorical") or [default])[k % max(1, len(tokens.get("categorical") or [1]))]))
                                   for k, c in enumerate(names)], dtype=np.float32)
        self.cls = np.array([names.index(c) for c in classes], dtype=np.int32)
        self.names = names
        self.flash_rgb = np.array(_hex_rgb(resolve_colour(cfg["flash_colour"]) if cfg.get("flash_colour") else tokens.get("label") or tokens.get("background")),
                                  dtype=np.float32)
        self.flash_s = float(cfg.get("flash_s", 0.6))
        self.glow = bool(cfg.get("glow", True)) and self.flash_s > 0
        self.opacity = float(cfg.get("opacity", 1.0))
        self.ss = int(cfg.get("supersample", 3))
        W, H = size
        self.size = (W, H)
        big = Image.new("I", (W * self.ss, H * self.ss), 0)
        d = ImageDraw.Draw(big)
        order = np.argsort(np.where(np.isfinite(self.born), self.born, -1e18), kind="stable")
        for i in order:
            geom = g.geometry.iloc[int(i)]
            if geom is None or geom.is_empty:
                continue
            fid = int(i) + 1
            for part in getattr(geom, "geoms", [geom]):
                if part.geom_type == "Polygon":
                    ring = [tuple(v * self.ss for v in to_px(x, y)) for x, y in part.exterior.coords]
                    if len(ring) >= 3:
                        d.polygon(ring, fill=fid)
                    for hole in part.interiors:
                        hr = [tuple(v * self.ss for v in to_px(x, y)) for x, y in hole.coords]
                        if len(hr) >= 3:
                            d.polygon(hr, fill=0)
                elif part.geom_type == "Point":
                    cx, cy = (v * self.ss for v in to_px(part.x, part.y))
                    r = 1.5 * self.ss
                    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fid)
        self.ids = np.asarray(big, dtype=np.int32)
        self.drawn = int(len(np.unique(self.ids)) - (1 if (self.ids == 0).any() else 0))

    def table(self, t: float) -> np.ndarray:
        """(n + 1) x 4 float32 RGBA (0-255, alpha 0-1) for every feature at time t; row 0 is the ground (transparent)."""
        lut = np.zeros((self.n + 1, 4), dtype=np.float32)
        alive = (self.born <= t) & (t < self.died)
        base = self.class_rgb[self.cls]
        if self.flash_s > 0:
            age = np.where(alive, t - self.born, np.inf)
            u = np.clip(age / self.flash_s, 0.0, 1.0)[:, None]
            rgb = self.flash_rgb[None, :] * (1 - u) + base * u
        else:
            rgb = base
        lut[1:, :3] = rgb
        lut[1:, 3] = np.where(alive, self.opacity, 0.0)
        return lut

    def counts(self, t: float) -> dict:
        alive = (self.born <= t) & (t < self.died)
        out = {"alive": int(alive.sum())}
        for k, name in enumerate(self.names):
            out[name] = int((alive & (self.cls == k)).sum())
        return out

    def draw(self, base, t: float):
        from PIL import Image, ImageFilter
        lut = self.table(t)
        px = lut[self.ids]                                        # (H*ss, W*ss, 4)
        H, W = self.size[1], self.size[0]
        ss = self.ss
        a = px[..., 3:4]
        pre = np.concatenate([px[..., :3] * a, a], axis=-1)       # premultiplied, then averaged down to the frame
        pre = pre.reshape(H, ss, W, ss, 4).mean(axis=(1, 3))
        alpha = pre[..., 3:4]
        rgb = np.where(alpha > 1e-6, pre[..., :3] / np.maximum(alpha, 1e-6), 0.0)
        over = np.concatenate([rgb, alpha * 255.0], axis=-1).clip(0, 255).astype(np.uint8)
        im = base.convert("RGBA")
        if self.glow:
            fl = (self.born <= t) & (t - self.born < self.flash_s) & (t < self.died)
            if fl.any():
                g = np.zeros((self.n + 1,), dtype=np.float32)
                g[1:] = np.where(fl, 1.0 - (t - self.born) / self.flash_s, 0.0)
                m = g[self.ids].reshape(H, ss, W, ss).mean(axis=(1, 3))
                glow = Image.fromarray((m * 160).clip(0, 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(radius=max(1.5, H / 400)))
                layer = Image.new("RGBA", im.size, tuple(int(v) for v in self.flash_rgb) + (0,))
                layer.putalpha(glow)
                im.alpha_composite(layer)
        im.alpha_composite(Image.fromarray(over, "RGBA"))
        return im.convert("RGB")
