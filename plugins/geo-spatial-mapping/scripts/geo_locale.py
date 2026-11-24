"""geo_locale: where a map is, and what that means for it (1.5.0, R3-UNITS, R3-CRS, R3-LOC).

    country_at(lon, lat)      the ISO 3166-1 alpha-2 code under a point: the bundled Natural Earth 10m admin-1 (simplified), else the 110m
                              countries, else the nearest admin-1 unit within 25 km (a coastal city centre on the water); None at sea
    country(iso)              its row of data/countries.yaml: name, M49 region, units, locale, paper, admin words
    units_at, locale_at       the distance units and the date style there (miles and en-US in the US, Liberia and Myanmar; km and en-GB elsewhere)
    date_text(d, locale)      "November 10, 2026" (en-US), "10 November 2026" (en-GB), "2026-11-10" (iso)
    national_crs(iso, purpose, lon)   the country's preset (data/national-crs.yaml), or None

No code path here assumes the United States: the US is one row of the table like any other.
"""
from __future__ import annotations

import math

import datetime as dt
import functools
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

LOCALES = ("en-US", "en-GB", "iso")
NEAREST_KM = 25.0


@functools.lru_cache(maxsize=1)
def table() -> dict:
    return gc.load_yaml(gc.plugin_root() / "data" / "countries.yaml")["countries"]


@functools.lru_cache(maxsize=1)
def crs_table() -> dict:
    return gc.load_yaml(gc.plugin_root() / "data" / "national-crs.yaml")


def country(iso: str | None) -> dict | None:
    return table().get(str(iso).upper()) if iso else None


@functools.lru_cache(maxsize=1)
def _admin1():
    import geopandas as gpd
    import shapely
    g = gpd.read_parquet(gc.plugin_root() / "assets" / "basemaps" / "ne_10m_admin1_loc.parquet")
    return g, shapely.STRtree(g.geometry.values)


@functools.lru_cache(maxsize=1)
def _admin0():
    import geopandas as gpd
    import shapely
    g = gpd.read_parquet(gc.plugin_root() / "assets" / "basemaps" / "ne_110m_countries.parquet")
    return g, shapely.STRtree(g.geometry.values)


def admin1_at(lon: float, lat: float) -> dict | None:
    """The admin-1 unit under a point (its row as a dict, with the geometry), or the nearest within NEAREST_KM."""
    import shapely
    g, tree = _admin1()
    p = shapely.Point(float(lon), float(lat))
    idx = tree.query(p, predicate="intersects")
    if not len(idx):
        near = tree.query_nearest(p, max_distance=NEAREST_KM / 111.0)
        if not len(near):
            return None
        idx = near
    r = g.iloc[int(idx[0])]
    return {k: r[k] for k in g.columns}


@functools.lru_cache(maxsize=4096)
def country_at(lon: float, lat: float) -> str | None:
    a = admin1_at(round(float(lon), 4), round(float(lat), 4))
    if a is not None and a.get("iso_a2") and str(a["iso_a2"]) not in ("-1", "-99", "None"):
        return str(a["iso_a2"]).upper()
    import shapely
    g, tree = _admin0()
    idx = tree.query(shapely.Point(float(lon), float(lat)), predicate="intersects")
    for i in idx:
        v = str(g.iloc[int(i)].get("ISO_A2_EH") or g.iloc[int(i)].get("ISO_A2") or "")
        if len(v) == 2 and v.isalpha():
            return v.upper()
    return None


def units_at(lon: float, lat: float) -> str:
    c = country(country_at(lon, lat))
    return (c or {}).get("units", "km")


def locale_for(iso: str | None) -> str:
    return (country(iso) or {}).get("locale", "en-GB")


def date_text(d: dt.date, locale: str = "en-US") -> str:
    if locale == "iso":
        return d.isoformat()
    if locale == "en-GB":
        return f"{d.day} {d.strftime('%B')} {d.year}"
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def admin_word(iso: str | None, level: int = 1) -> str:
    words = (country(iso) or {}).get("admin") or ["region"]
    return words[min(level, len(words)) - 1]


WORLD_SPAN_DEG = 120.0       # a map wider than this (in longitude) shows no one country: no country under the centre (1.14.0)


def plan_country(plan) -> str | None:
    """The country under the centre of a plan's window (cached on the plan); None when the plan has no window, or for a map of the world or
    an ocean (wider than WORLD_SPAN_DEG), which has no one country.

    1.14.0 (Day 13 issue 2): the centre is the middle of the map's own frame, taken back to longitude and latitude. 1.13 took the middle of the
    frame's longitude-latitude box, which for a world map centred on 150 degrees east is 0 degrees, so "the country under the centre" was
    Ghana and the dates came out in its style."""
    cache = getattr(plan, "__dict__", {})
    if "_country" not in cache:
        try:
            import map_plan as mp
            from pyproj import Transformer
            w = mp._wgs84_extent(plan)
            x0, y0, x1, y1 = plan.extent
            lon, lat = Transformer.from_crs(plan.crs, "EPSG:4326", always_xy=True).transform((x0 + x1) / 2, (y0 + y1) / 2)
            if w[2] - w[0] >= WORLD_SPAN_DEG or not (math.isfinite(lon) and math.isfinite(lat)):
                cache["_country"] = None
            else:
                cache["_country"] = country_at(lon, lat)
        except Exception:  # noqa: BLE001 - a chart without a map window
            cache["_country"] = None
    return cache.get("_country")


def plan_locale(plan) -> str:
    """locale: auto (the country under the centre), en-US, en-GB or iso; the brand pack may set a default (`locale:` in its PACK.json).
    A plan with no window (a chart) keeps en-US, the form every stamp had before 1.5.0."""
    spec = getattr(plan, "spec", None) or {}
    pack = (getattr(plan, "pack", None) or {}).get("pack") or {}
    asked = str(spec.get("locale") or pack.get("locale") or "auto").strip()
    if asked in LOCALES:
        return asked
    iso = plan_country(plan)
    return locale_for(iso) if iso else "en-US"


def plan_date_text(plan, d: dt.date) -> str:
    return date_text(d, plan_locale(plan))


def locale_finding(plan, sb_info: dict | None = None) -> list[dict]:
    """MAP-LOC-001 (INFO): the locale and units chosen, and why; MAP-LOC-002 (WARN): the window crosses countries with other units."""
    iso = plan_country(plan)
    loc = plan_locale(plan)
    spec = getattr(plan, "spec", None) or {}
    why_loc = (f"locale {loc}: asked" if str(spec.get("locale") or "auto") != "auto"
               else f"locale {loc}: {'the country under the centre is ' + ((country(iso) or {}).get('name') or iso) if iso else 'no country under the centre'}")
    out = [{"id": "MAP-LOC-001", "severity": "INFO", "message": why_loc + (f"; units {sb_info['units']}: {sb_info['units_reason']}"
                                                                           if sb_info and sb_info.get("units_reason") else ""),
            "detail": {"country": iso, "locale": loc, "units": (sb_info or {}).get("units")}}]
    mixed = (sb_info or {}).get("units_mixed") or []
    if mixed:
        named = [(country(c) or {}).get("name") or c for c in mixed]       # 1.13.0 (Day 12 issue 10): Canada, not CA (which reads as California)
        out.append({"id": "MAP-LOC-002", "severity": "WARN",
                    "message": (f"the window also covers {', '.join(named)}, which use{'s' if len(mixed) == 1 else ''} other distance units; the scale bar "
                                f"is in {sb_info['units']}, the units of the country under the centre (set elements.scale_bar.units to choose)"),
                    "detail": {"other_countries": mixed}})
    return out


def countries_in(bbox, n: int = 5) -> list[str]:
    """The countries a WGS84 window covers, read on an n x n grid and the centre (sea points skipped), the centre's country first."""
    x0, y0, x1, y1 = (float(v) for v in bbox)
    pts = [((x0 + x1) / 2, (y0 + y1) / 2)] + [(x0 + (x1 - x0) * i / (n - 1), y0 + (y1 - y0) * j / (n - 1)) for i in range(n) for j in range(n)]
    out: list[str] = []
    for lo, la in pts:
        c = country_at(round(lo, 4), round(la, 4))
        if c and c not in out:
            out.append(c)
    return out


def national_crs(iso: str | None, purpose: str, lon: float) -> dict | None:
    """The country's preset for a purpose: {crs, name, equal_area}. thematic takes `thematic` when the country has one, else its grid."""
    if not iso:
        return None
    e = (crs_table().get("countries") or {}).get(str(iso).upper())
    if not e:
        return None
    if purpose == "thematic" and e.get("thematic"):
        return {"crs": e["thematic"], "name": "ETRS89-extended / LAEA Europe" if e["thematic"] == "EPSG:3035" else e["thematic"], "equal_area": True}
    if e.get("utm"):
        zone = int((float(lon) + 180) // 6) + 1
        code = e["utm"].get(zone) or e["utm"].get(str(zone))
        if not code:
            return None
        return {"crs": code, "name": f"{e.get('name', 'UTM')} zone {zone}", "equal_area": False}
    return {"crs": e["crs"], "name": e.get("name", e["crs"]), "equal_area": bool(e.get("equal_area"))}
