#!/usr/bin/env python3
"""ground_detail: detailed land and water under a map, from the finest provider that covers the whole window (0.11.0 US; 1.5.0 anywhere).

    ground_detail.py prepare --spec MAP.yaml        fetch, once, what the map's window needs into the cache
    ground_detail.py prepare --bbox=W,S,E,N [--scale 150000] [--provider ID]   the same for a lon/lat box
    ground_detail.py status  --spec MAP.yaml        which provider the map takes, what is cached and what is missing; fetches nothing

1.5.0 (R3-GND): the providers are in data/ground-providers.yaml, finest first: us-tiger (a window wholly inside the United States), global-overture
(anywhere, finer than 1:1,500,000: the Overture base theme's land and water, read per z8 quadkey tile by overture_io), global-osm-simplified (1:10M
to 1:1.5M: the OSM simplified land polygons with the Natural Earth 10m lakes) and bundled-ne. Per window the build takes the finest provider whose
coverage holds the WHOLE window, at one resolution for land and water (MAP-CART-024), and says which and why (MAP-GND-001); a finer provider that is
not cached is skipped for the next and said (MAP-GND-002). A Seattle and Vancouver window is not wholly inside the US, so it takes Overture for all
its ground, not TIGER on one side and OSM on the other. The TIGER provider, as before:

The bundled ground is Natural Earth: its 1:10,000,000 coast is kilometres off on a map of a city. Finer than DETAIL_MAX_DENOMINATOR (1:1,500,000)
a US map draws instead:
  water  TIGER/Line area water for each county in the window (`ref.context.us.water-area.tiger`, one file a county): sounds, bays, lakes,
         reservoirs and wide rivers at the full TIGER detail, small ponds dropped by size at the map's scale
  land   TIGER/Line county subdivisions for each state in the window (`ref.boundary.us.cousub.tiger`, one file a state), which cover the land and
         its coastal and inland water, with that same TIGER water cut out (the build does the cut): the land's edge IS the water's edge, at one
         resolution. Rule (0.12.0): land and water on a map always come from one source at one resolution (MAP-CART-024). The 1:500,000
         counties used in 0.11.0 drew a generalized coast beside a detailed water edge.
  states the 1:5,000,000 state lines (`ref.boundary.us.state.cb5m`, bundled)
All three are public domain (U.S. Census Bureau). `prepare` fetches them through ref_fetch (the entries' own HTTPS URLs, hashes where stated) into
.geo-mapping/cache/library/; a map build reads only that cache and never goes online. A window with land outside the US (a map at the Canadian or
Mexican border) is not TIGER's: it takes the next provider that covers it whole.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

DETAIL_MAX_DENOMINATOR = 1_500_000
LAND_REF = "ref.boundary.us.cousub.tiger"                # per state (variant = two-digit FIPS); 0.11.0 used the 1:500,000 counties
RESOLUTION = "TIGER/Line (full resolution)"              # what both the land and the water of detailed ground are (MAP-CART-024)
WATER_REF = "ref.context.us.water-area.tiger"
COUNTY_INDEX_REF = "ref.boundary.us.county.cb"          # bundled: which counties a window touches
STATES_REF = "ref.boundary.us.state.cb5m"
MAX_COUNTIES = 60
MIN_WATER_MM2 = 0.8                                       # a pond smaller than this on the page is left out


class DetailError(Exception):
    pass


def counties_in(window_ll) -> list[str]:
    """Five-digit county FIPS codes whose (bundled, generalized) outline meets the lon/lat window."""
    import map_plan as mp
    from shapely.geometry import box
    g, _ = mp.load_ref_layer(COUNTY_INDEX_REF)
    g = g.to_crs("EPSG:4326") if g.crs is not None and g.crs.to_epsg() != 4326 else g
    w = box(*window_ll).buffer(0.02)
    hit = g[g.geometry.intersects(w)]
    col = "GEOID" if "GEOID" in hit.columns else next(c for c in hit.columns if c.upper() == "GEOID")
    return sorted(str(v).zfill(5) for v in hit[col])


def _entry(eid: str) -> dict:
    import ref_fetch as rf
    return rf.find_entry(eid)


def status(window_ll) -> dict:
    import ref_fetch as rf
    fips = counties_in(window_ll)
    states = sorted({f[:2] for f in fips})
    le, we = _entry(LAND_REF), _entry(WATER_REF)
    land = {s: rf.cached_path(le, s) for s in states}
    water = {f: rf.cached_path(we, f) for f in fips}
    return {"window": [round(v, 5) for v in window_ll], "counties": fips, "states": states,
            "land": {s: (str(p) if p else None) for s, p in land.items()},
            "water": {f: (str(p) if p else None) for f, p in water.items()},
            "missing": [f"{LAND_REF} --variant {s}" for s, p in land.items() if p is None] + [f"{WATER_REF} --variant {f}" for f, p in water.items() if p is None]}


def prepare(window_ll, refresh: bool = False, http=None) -> dict:
    import ref_fetch as rf
    st = status(window_ll)
    if len(st["counties"]) > MAX_COUNTIES:
        raise DetailError(f"the window touches {len(st['counties'])} counties; detailed ground is for metro and city maps (at most {MAX_COUNTIES} counties)")
    done = []
    for s_, p in st["land"].items():
        if refresh or p is None:
            r = rf.fetch_entry(LAND_REF, variant=s_, refresh=refresh, http=http)
            done.append({"entry": LAND_REF, "variant": s_, "path": r.get("path") or r.get("cache")})
    for f, p in st["water"].items():
        if refresh or p is None:
            r = rf.fetch_entry(WATER_REF, variant=f, refresh=refresh, http=http)
            done.append({"entry": WATER_REF, "variant": f, "path": r.get("path") or r.get("cache")})
    after = status(window_ll)
    return {"fetched": done, "counties": after["counties"], "missing": after["missing"]}


def load(window_ll, plan_crs, scale_den: float, map_width_mm: float, visible_width_m: float):
    """What a build draws, from the cache only: {land_ll, water (GeoDataFrame in plan_crs), entries, missing, counties}. Nothing is fetched."""
    import geopandas as gpd
    import shapely
    from shapely.geometry import box
    st = status(window_ll)
    out = {"land_ll": None, "water": None, "entries": [], "missing": st["missing"], "counties": st["counties"], "resolution": RESOLUTION}
    wbox = box(*window_ll)
    if st["water"] and any(p is None for p in st["water"].values()):
        return out                                      # land without all its water would draw water as land: all of a window, or none of it
    lparts = [gpd.read_parquet(p) for p in st["land"].values() if p]
    if lparts and all(st["land"].values()):
        import pandas as pd
        land = gpd.GeoDataFrame(pd.concat([x.to_crs("EPSG:4326") for x in lparts], ignore_index=True), crs="EPSG:4326")
        near = land[land.geometry.intersects(wbox)]
        if len(near):
            # the subdivisions cover land and water; the build cuts the TIGER water out, so the land's edge is the water's edge
            out["land_ll"] = shapely.make_valid(shapely.union_all(shapely.intersection(near.geometry.values, wbox)))
            out["entries"].append(_entry(LAND_REF))
    parts = [gpd.read_parquet(p) for p in st["water"].values() if p]
    if parts:
        import pandas as pd
        w = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)
        w = w.to_crs("EPSG:4326") if w.crs is not None and w.crs.to_epsg() != 4326 else w
        w = w[w.geometry.intersects(wbox)].copy()
        w["geometry"] = shapely.make_valid(shapely.intersection(w.geometry.values, wbox))
        w = w[~w.geometry.is_empty].to_crs(plan_crs)
        mm_per_m = map_width_mm / max(visible_width_m, 1e-9)              # page millimetres per ground metre
        w = w[w.geometry.area * mm_per_m ** 2 >= MIN_WATER_MM2]
        # TIGER cuts water at county lines (Puget Sound is a dozen pieces): merged into one shape, so no county line is drawn across open water
        merged = shapely.make_valid(shapely.union_all(w.geometry.values)) if len(w) else None
        out["water"] = gpd.GeoDataFrame({"geometry": [merged] if merged is not None and not merged.is_empty else []}, crs=w.crs)
        out["entries"].append(_entry(WATER_REF))
    return out


# ------------------------------------------------------------------------------------------------------------------------ 1.5.0: providers
OVERTURE_REF = "ref.context.world.ground.overture"
OSM_REF = "ref.context.world.land.osm-simplified"
LAKES_REF = "ref.context.world.lakes.ne10"
OVERTURE_RES = "Overture base theme (OSM coastline and water)"
OSM_RES = "OSM simplified land polygons, Natural Earth 1:10m lakes"
NE_RES = "Natural Earth 1:10m"


def providers() -> list[dict]:
    return gc.load_yaml(gc.plugin_root() / "data" / "ground-providers.yaml")["providers"]


def _in_scale(p: dict, den: float | None) -> bool:
    if den is None:
        return True
    if p.get("max_denominator") and den >= float(p["max_denominator"]):
        return False
    return not (p.get("min_denominator") and den < float(p["min_denominator"]))


def us_holds(window_ll) -> bool:
    """Whether the window's land lies wholly inside the United States (TIGER's coverage): the 10m land of the window, less the US states with
    a 2 km margin, under 0.5 percent of it."""
    import shapely
    import map_plan as mp
    from shapely.geometry import box
    try:
        import geo_locale as gl
        if any(c != "US" for c in gl.countries_in(window_ll)):
            return False
    except Exception:  # noqa: BLE001
        pass
    wbox = box(*window_ll)
    ocean, _ = mp.load_ref_layer(mp.BASEMAP_REFS["ocean"])
    ocean = ocean.to_crs("EPSG:4326") if ocean.crs is not None and not ocean.crs.equals("EPSG:4326") else ocean
    land = shapely.difference(wbox, shapely.union_all(shapely.intersection(ocean.geometry.values, wbox)))
    if land.area <= 0:
        return True
    states, _ = mp.load_ref_layer(mp.BASEMAP_REFS["states"])
    us = shapely.union_all(states.to_crs("EPSG:4326").geometry.values).buffer(0.02)
    return shapely.difference(land, us).area / land.area <= 0.005


def overture_release() -> str:
    return str((_entry(OVERTURE_REF).get("delivery") or {}).get("release"))


def overture_status(window_ll) -> dict:
    import overture_io as ov
    rel = overture_release()
    tiles = ov.tiles_for(window_ll)
    have = {q: ov.tile_path(rel, q) for q in tiles}
    return {"provider": "global-overture", "release": rel, "tiles": tiles,
            "missing": [f"{OVERTURE_REF} --variant {q}" for q, p in have.items() if not p.exists()]}


def osm_status() -> dict:
    import ref_fetch as rf
    p = rf.cached_path(_entry(OSM_REF))
    return {"provider": "global-osm-simplified", "path": str(p) if p else None, "missing": [] if p else [OSM_REF]}


def provider_status(pid: str, window_ll) -> dict:
    if pid == "us-tiger":
        st = status(window_ll)
        if len(st["counties"]) > MAX_COUNTIES:
            st["missing"] = st["missing"] or [f"{len(st['counties'])} counties (at most {MAX_COUNTIES})"]
            st["too_large"] = True
        return {"provider": pid, **st}
    if pid == "global-overture":
        return overture_status(window_ll)
    if pid == "global-osm-simplified":
        return osm_status()
    return {"provider": pid, "missing": []}


def choose(window_ll, den: float | None, *, cached_only: bool = True, asked: str | None = None) -> dict:
    """The provider for a window: the finest whose coverage holds the whole window and whose scales include the map's, and, with cached_only,
    whose files are cached. -> {provider, why, skipped: [{provider, why}], status}."""
    skipped = []
    us = None
    for p in providers():
        pid = p["id"]
        if asked and asked != "auto" and pid != asked and pid != "bundled-ne":
            continue
        if not _in_scale(p, den):
            continue
        if p.get("coverage") == "us":
            us = us_holds(window_ll) if us is None else us
            if not us:
                skipped.append({"provider": pid, "why": "the window is not wholly inside the United States"})
                continue
        st = provider_status(pid, window_ll)
        if st.get("too_large"):
            skipped.append({"provider": pid, "why": f"the window is larger than detailed ground is for ({st['missing'][0]})"})
            continue
        if cached_only and st.get("missing"):
            skipped.append({"provider": pid, "why": "not cached for this window (" + "; ".join(st["missing"][:3]) + ("; ..." if len(st["missing"]) > 3 else "") + ")",
                            "missing": st["missing"]})
            continue
        scale = f"1:{den:,.0f}" if den else "this scale"
        why = {"us-tiger": f"the window lies wholly inside the United States and the map is {scale}: TIGER/Line, public domain",
               "global-overture": f"the map is {scale}, finer than 1:1,500,000" + (", and the window is not wholly inside the United States" if us is False else "")
                                  + ": the Overture base theme (OSM-derived, ODbL)",
               "global-osm-simplified": f"the map is {scale}, between 1:10,000,000 and 1:1,500,000: the OSM simplified land polygons (ODbL)",
               "bundled-ne": "Natural Earth, bundled" + (": nothing finer is cached for this window" if skipped else f" (right at {scale})")}[pid]
        return {"provider": pid, "why": why, "skipped": skipped, "status": st}
    return {"provider": "bundled-ne", "why": "Natural Earth, bundled", "skipped": skipped, "status": {}}


def load_overture(window_ll, plan_crs, map_width_mm: float, visible_width_m: float) -> dict:
    """Overture land and water for the window, from the cached tiles only. -> {land_ll, water (in plan_crs), entries, missing, resolution}."""
    import geopandas as gpd
    import pandas as pd
    import shapely
    from shapely.geometry import box
    st = overture_status(window_ll)
    out = {"land_ll": None, "water": None, "entries": [], "missing": st["missing"], "resolution": OVERTURE_RES, "tiles": st["tiles"]}
    if st["missing"]:
        return out
    import overture_io as ov
    parts = [gpd.read_parquet(ov.tile_path(st["release"], q)) for q in st["tiles"]]
    parts = [p for p in parts if len(p)]
    wbox = box(*window_ll)
    g = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs="EPSG:4326") if parts else gpd.GeoDataFrame({"kind": []}, geometry=[], crs="EPSG:4326")
    g = g[g.geometry.intersects(wbox)]
    land = g[g["kind"] == "land"] if len(g) else g
    water = g[g["kind"] == "water"] if len(g) else g
    out["land_ll"] = shapely.make_valid(shapely.union_all(shapely.intersection(land.geometry.values, wbox))) if len(land) else shapely.Polygon()
    w = water.copy()
    if len(w):
        w["geometry"] = shapely.make_valid(shapely.intersection(w.geometry.values, wbox))
        w = w[~w.geometry.is_empty].to_crs(plan_crs)
        mm_per_m = map_width_mm / max(visible_width_m, 1e-9)
        w = w[w.geometry.area * mm_per_m ** 2 >= MIN_WATER_MM2]
    merged = shapely.make_valid(shapely.union_all(w.geometry.values)) if len(w) else None
    out["water"] = gpd.GeoDataFrame({"geometry": [merged] if merged is not None and not merged.is_empty else []}, crs=plan_crs)
    e = _entry(OVERTURE_REF)
    out["entries"] = [e]
    return out


def load_osm(window_ll) -> dict:
    """The OSM simplified land for the window (lon/lat), from the cached file only; the water is the inverse with the Natural Earth 10m lakes."""
    import geopandas as gpd
    import shapely
    from shapely.geometry import box
    st = osm_status()
    out = {"land_ll": None, "water": None, "entries": [], "missing": st["missing"], "resolution": OSM_RES}
    if st["missing"]:
        return out
    from pyproj import Transformer
    wbox = box(*window_ll)
    t = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    xs, ys = t.transform([window_ll[0], window_ll[2], window_ll[0], window_ll[2]], [window_ll[1], window_ll[1], window_ll[3], window_ll[3]])
    g = gpd.read_parquet(st["path"], bbox=None)
    g = g.cx[min(xs):max(xs), min(ys):max(ys)] if g.crs is not None and not g.crs.is_geographic else g.cx[window_ll[0]:window_ll[2], window_ll[1]:window_ll[3]]
    g = g.to_crs("EPSG:4326") if g.crs is not None and not g.crs.equals("EPSG:4326") else g
    out["land_ll"] = shapely.make_valid(shapely.union_all(shapely.intersection(g.geometry.values, wbox))) if len(g) else shapely.Polygon()
    out["entries"] = [_entry(OSM_REF)]
    return out


def prepare_window(window_ll, den: float | None, *, refresh: bool = False, http=None, provider: str | None = None, cap_bytes: int | None = None) -> dict:
    """Fetch, once, what the window's provider needs: the finest provider that covers the whole window at this scale (not only what is cached).
    Overture tiles are read one at a time through ref_fetch (host-side through the MCP tool, as every fetch), within the window's byte cap."""
    import ref_fetch as rf
    sel = choose(window_ll, den, cached_only=False, asked=provider)
    pid = sel["provider"]
    out = {"provider": pid, "why": sel["why"], "fetched": [], "skipped": sel["skipped"]}
    if pid == "us-tiger":
        r = prepare(window_ll, refresh, http=http)
        out.update(r)
    elif pid == "global-overture":
        import overture_io as ov
        e = _entry(OVERTURE_REF)
        cap = int(cap_bytes or float((e.get("delivery") or {}).get("cap_mb", 300)) * 1e6)
        used = 0
        for q in overture_status(window_ll)["tiles"]:
            if ov.tile_path(overture_release(), q).exists() and not refresh:
                continue
            if used >= cap:
                raise ov.CapExceeded(f"the window needs more than {cap / 1e6:,.0f} MB of Overture data; nothing more was read (MAP-GND-003)")
            r = ov.fetch_tile(q, overture_release(), http=http, refresh=refresh, cap_bytes=cap - used)
            used += int(r.get("bytes") or 0)
            out["fetched"].append({"entry": OVERTURE_REF, "variant": q, "path": r["path"], "bytes": r.get("bytes")})
        out["bytes"] = used
        out["missing"] = overture_status(window_ll)["missing"]
    elif pid == "global-osm-simplified":
        if refresh or osm_status()["missing"]:
            r = rf.fetch_entry(OSM_REF, refresh=refresh, http=http)
            out["fetched"].append({"entry": OSM_REF, "path": r.get("path")})
        out["missing"] = osm_status()["missing"]
    else:
        out["missing"] = []
    return out


def window_of_spec(spec: str):
    import map_plan as mp
    plan = mp.prepare(spec)
    return mp.detail_window(plan), mp.scale_denominator(plan)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("prepare", "status"))
    ap.add_argument("--spec")
    ap.add_argument("--bbox")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--scale", type=float, help="the map's scale denominator with --bbox (default: none, every provider is considered)")
    ap.add_argument("--provider", help="a provider id from data/ground-providers.yaml (default: the finest that covers the window)")
    a = ap.parse_args(argv)
    try:
        if a.spec:
            win, den = window_of_spec(a.spec)
        elif a.bbox:
            win, den = tuple(float(v) for v in a.bbox.split(",")), a.scale
        else:
            print("ground_detail: give --spec MAP.yaml or --bbox=W,S,E,N", file=sys.stderr)
            return gc.EXIT_USAGE
        if a.cmd == "status":
            sel = choose(win, den, cached_only=True, asked=a.provider)
            best = choose(win, den, cached_only=False, asked=a.provider)
            res = {"window": [round(v, 5) for v in win], "scale_denominator": round(den) if den else None, "provider": sel["provider"], "why": sel["why"],
                   "finest": best["provider"], "skipped": sel["skipped"], "status": best.get("status")}
        else:
            res = prepare_window(win, den, refresh=a.refresh, provider=a.provider)
        print(json.dumps(res, indent=2, default=str))
        return gc.EXIT_OK
    except DetailError as ex:
        print(f"ground_detail: {ex}", file=sys.stderr)
        return gc.EXIT_USAGE
    except Exception as ex:  # noqa: BLE001 - a cap or a refused read is said, never a traceback
        if type(ex).__name__ == "CapExceeded":
            print(json.dumps({"findings": [{"id": "MAP-GND-003", "severity": "BLOCK", "message": str(ex) + "; nothing more was read"}]}, indent=2))
            print(f"ground_detail: {ex}", file=sys.stderr)
            return gc.EXIT_REFUSED
        if type(ex).__name__ == "Refused":
            print(f"ground_detail: {ex}", file=sys.stderr)
            return gc.EXIT_REFUSED
        raise


if __name__ == "__main__":
    sys.exit(main())
