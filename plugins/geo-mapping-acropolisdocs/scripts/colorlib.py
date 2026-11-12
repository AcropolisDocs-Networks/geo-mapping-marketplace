"""Color math used at brand-import time, by the lints, and for the one derived palette (never inside the renderer).

The bivariate grid (M15) is the only colour set computed from the tokens rather than picked from them: `bivariate_grid` blends the two arms of
the brand's diverging ramp in Oklab, and the brand lint recomputes the same grid to allow its colours. Contrast follows WCAG 2.x relative luminance. Color-vision-deficiency (CVD) simulation uses the
Machado, Oliveira and Fernandes (2009) matrices at full severity, applied in linear RGB, and
distance is CIE76 delta E in Lab. All functions are pure and dependency-free.
"""
from __future__ import annotations

import colorsys
import math

_PROTAN = ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998))
_DEUTAN = ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.011820, 0.042940, 0.968881))
_TRITAN = ((1.255528, -0.076749, -0.178779), (-0.078411, 0.930809, 0.147602), (0.004733, 0.691367, 0.303900))
CVD_MATRICES = {"protan": _PROTAN, "deutan": _DEUTAN, "tritan": _TRITAN}


def hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError(f"not a hex color: {h!r}")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def rgb_to_hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in rgb)


def norm_hex(h: str) -> str:
    return rgb_to_hex(hex_to_rgb(h))


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _unlin(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def luminance(h: str) -> float:
    r, g, b = (_lin(c) for c in hex_to_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def hls(h: str) -> tuple[float, float, float]:
    """Return (hue 0-360, lightness 0-1, saturation 0-1)."""
    r, g, b = hex_to_rgb(h)
    hh, ll, ss = colorsys.rgb_to_hls(r, g, b)
    return hh * 360.0, ll, ss


def from_hls(hue: float, light: float, sat: float) -> str:
    r, g, b = colorsys.hls_to_rgb((hue % 360.0) / 360.0, max(0.0, min(1.0, light)), max(0.0, min(1.0, sat)))
    return rgb_to_hex((r, g, b))


def is_grey(h: str, sat_threshold: float = 0.12) -> bool:
    return hls(h)[2] < sat_threshold


def mix(a: str, b: str, t: float) -> str:
    ra, rb = hex_to_rgb(a), hex_to_rgb(b)
    return rgb_to_hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))


def lab(h: str, cvd: str | None = None) -> tuple[float, float, float]:
    r, g, b = (_lin(c) for c in hex_to_rgb(h))
    if cvd:
        m = CVD_MATRICES[cvd]
        r, g, b = (max(0.0, min(1.0, m[i][0] * r + m[i][1] * g + m[i][2] * b)) for i in range(3))
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    xn, yn, zn = 0.95047, 1.0, 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x / xn), f(y / yn), f(z / zn)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a: str, b: str, cvd: str | None = None) -> float:
    la, lb = lab(a, cvd), lab(b, cvd)
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(la, lb)))


def cvd_min_delta_e(a: str, b: str) -> float:
    """Smallest delta E between two colors across normal vision and the three CVD types."""
    return min(delta_e(a, b, c) for c in (None, "protan", "deutan", "tritan"))


def oklab(h: str) -> tuple[float, float, float]:
    """Oklab (Ottosson 2020) of an sRGB hex: L 0-1, a and b about -0.4 to 0.4. Perceptually even enough to blend two ramps in."""
    r, g, b = (_lin(c) for c in hex_to_rgb(h))
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = (math.copysign(abs(v) ** (1 / 3), v) for v in (l, m, s))
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def from_oklab(L: float, a: float, b: float) -> str:
    """sRGB hex of an Oklab colour, clipped to the sRGB gamut."""
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    r = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    bb = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return rgb_to_hex(tuple(_unlin(v) for v in (r, g, bb)))


BIVARIATE_FLOOR_L = 0.10     # the Oklab lightness a cell high on both axes approaches but never reaches (light theme; 1 - this + 0.2 on a dark one)
BIVARIATE_LIFT = 0.06        # the low-low cell starts this share of the way along both arms, so it is not the land or the legend background
BIVARIATE_LIFT_DARK = 0.10   # ...more on a dark ground, where the neutral middle of the diverging ramp is the land itself


def bivariate_grid(base: str, x_end: str, y_end: str, n: int = 3, *, floor: float = BIVARIATE_FLOOR_L, lift: float | None = None) -> list[list[str]]:
    """An n x n bivariate grid from two ramps that start at the same neutral `base` (the middle of a diverging ramp): one runs to `x_end`, the
    other to `y_end` (its two ends). Cell [j][i] (row j is the y class, column i the x class, both from 0 = low) is base plus u of the way to
    x_end plus v of the way to y_end, added as vectors in Oklab (u, v run from `lift` to 1). Hue and chroma add as they are; the lightness drop
    (or, on a dark ground, the rise) adds too but is eased with tanh towards `floor`, so the cell high on both axes is the darkest and carries
    both hues without turning black. Pure and deterministic: the brand lint recomputes the same grid from the same tokens to allow its colours
    (MAP-BRAND-001), so the grid is never typed in anywhere."""
    if n < 2:
        raise ValueError("a bivariate grid needs at least 2 classes per axis")
    o0, ox, oy = oklab(base), oklab(x_end), oklab(y_end)
    light_ground = o0[0] > 0.5
    lift = (BIVARIATE_LIFT if light_ground else BIVARIATE_LIFT_DARK) if lift is None else lift
    D = (o0[0] - floor) if light_ground else (1.0 - floor + 0.2 - o0[0])
    D = max(D, 1e-6)
    out = []
    for j in range(n):
        row = []
        for i in range(n):
            u = lift + (1.0 - lift) * i / (n - 1)
            v = lift + (1.0 - lift) * j / (n - 1)
            drop = abs(u * (ox[0] - o0[0]) + v * (oy[0] - o0[0]))
            eased = D * math.tanh(drop / D)
            L = o0[0] - eased if light_ground else o0[0] + eased
            a = o0[1] + u * (ox[1] - o0[1]) + v * (oy[1] - o0[1])
            b = o0[2] + u * (ox[2] - o0[2]) + v * (oy[2] - o0[2])
            row.append(from_oklab(L, a, b))
        out.append(row)
    return out


def min_pair_delta_e(colors: list[str]) -> tuple[float, tuple[str, str] | None]:
    """The smallest delta E between any two of `colors` under normal vision and the three CVD types, and the pair."""
    best, pair = float("inf"), None
    for i, a in enumerate(colors):
        for b in colors[i + 1:]:
            d = cvd_min_delta_e(a, b)
            if d < best:
                best, pair = d, (a, b)
    return best, pair


def ensure_contrast(color: str, ground: str, minimum: float) -> tuple[str, bool]:
    """Move lightness away from the ground until contrast >= minimum. Returns (color, changed)."""
    if contrast(color, ground) >= minimum:
        return norm_hex(color), False
    hue, light, sat = hls(color)
    direction = -1.0 if luminance(ground) > 0.4 else 1.0
    for _ in range(60):
        light += direction * 0.02
        light = max(0.0, min(1.0, light))
        cand = from_hls(hue, light, sat)
        if contrast(cand, ground) >= minimum:
            return cand, True
        if light in (0.0, 1.0):
            break
    return from_hls(hue, light, sat), True
