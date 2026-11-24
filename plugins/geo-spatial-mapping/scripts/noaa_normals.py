"""noaa_normals: NOAA's U.S. Climate Normals 1991-2020 (monthly) as a point layer of stations for a region (3.1.0).

    noaa_normals.py fetch --region "US-WA" [--variables tavg,tmax,tmin,prcp] [--max-stations 60] [--out normals.parquet]
    noaa_normals.py stations --region "Puget Sound"            the stations in the region, from the inventory; nothing else fetched

The station list is NCEI's inventory (doc/inventory_30yr.txt, one line a station: id, latitude, longitude, elevation, state, name); each station's
monthly normals are one CSV at /data/normals-monthly/1991-2020/access/<id>.csv (MLY-TAVG-NORMAL, MLY-TMAX-NORMAL, MLY-TMIN-NORMAL in degrees
Fahrenheit, MLY-PRCP-NORMAL in inches). The layer has a column a variable a month (tavg_01 to tavg_12 ...) and an annual column (the mean of
the months for temperature, the sum for precipitation), with the units in the column metadata and the station's name, state and elevation.
Stations are taken nearest the region's centre first, at most --max-stations (60) a call, so a state map is one minute of requests, not
thousands. Public domain (NOAA NCEI); credit "NOAA NCEI U.S. Climate Normals 1991-2020". Every request goes through ogc_http.SafeHTTP.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

BASE = "https://www.ncei.noaa.gov/data/normals-monthly/1991-2020/"
INVENTORY = BASE + "doc/inventory_30yr.txt"
ACCESS = BASE + "access/{sid}.csv"
ENTRY = "ref.statistic.us.climate.noaa-normals"
VARIABLES = {"tavg": ("MLY-TAVG-NORMAL", "degF", "mean"), "tmax": ("MLY-TMAX-NORMAL", "degF", "mean"), "tmin": ("MLY-TMIN-NORMAL", "degF", "mean"),
             "prcp": ("MLY-PRCP-NORMAL", "in", "sum"), "snow": ("MLY-SNOW-NORMAL", "in", "sum")}
MAX_STATIONS = 60
ATTRIBUTION = "Climate: NOAA NCEI U.S. Climate Normals 1991-2020 (monthly), public domain."


class NormalsError(Exception):
    def __init__(self, message: str, code: int = 2, rule: str | None = None):
        super().__init__(message)
        self.code, self.rule = code, rule


def _http(http=None):
    import ogc_http as oh
    return http or oh.SafeHTTP(limits={"timeout_s": 60, "max_bytes": 20_000_000, "retries": 1})


def cache_root(cache_dir=None) -> Path:
    return (Path(cache_dir) if cache_dir else gc.workdir(create=True)) / "fetch" / "noaa-normals"


def parse_inventory(text: str) -> list[dict]:
    """The fixed-width inventory: ID (11), latitude, longitude, elevation (m), state, name."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            lat, lon, elev = float(parts[1]), float(parts[2]), float(parts[3])
        except ValueError:
            continue
        state = parts[4] if len(parts[4]) == 2 and parts[4].isalpha() else ""
        name = " ".join(parts[5:] if state else parts[4:])
        name = " ".join(w for w in name.split() if not (w.isdigit() or w in ("GSN", "HCN", "WMO", "CRN")))
        out.append({"id": parts[0], "lat": lat, "lon": lon, "elevation_m": elev, "state": state, "name": name.strip()})
    return out


def inventory(http=None, cache_dir=None) -> list[dict]:
    p = cache_root(cache_dir) / "inventory_30yr.txt"
    if not p.is_file():
        if os.environ.get("GMAP_OFFLINE") == "1":
            raise NormalsError("offline (GMAP_OFFLINE=1): the normals inventory is not cached", 3)
        r = _http(http).get(INVENTORY, expect="any", max_bytes=5_000_000)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(r.content)
    return parse_inventory(p.read_text(encoding="utf-8", errors="replace"))


def stations_in(region, http=None, cache_dir=None, max_stations: int = MAX_STATIONS) -> tuple[list[dict], int]:
    """The stations inside the region, nearest its centre first. -> (stations, how many the region holds)."""
    from shapely import contains_xy
    import numpy as np
    inv = inventory(http, cache_dir)
    W, S, E, N = region.bounds
    box = [s for s in inv if W <= s["lon"] <= E and S <= s["lat"] <= N]
    if not box:
        return [], 0
    inside = contains_xy(region.geometry, np.array([s["lon"] for s in box]), np.array([s["lat"] for s in box]))
    sel = [s for s, k in zip(box, inside, strict=True) if k]
    c = region.geometry.centroid
    sel.sort(key=lambda s: math.hypot(s["lon"] - c.x, (s["lat"] - c.y) * 1.0))
    return sel[:max_stations], len(sel)


def station_normals(sid: str, variables, http=None, cache_dir=None) -> dict | None:
    """One station's monthly normals for the variables, or None when it publishes none of them."""
    import pandas as pd
    p = cache_root(cache_dir) / "access" / f"{sid}.csv"
    if not p.is_file():
        r = _http(http).get(ACCESS.format(sid=sid), expect="any", max_bytes=2_000_000)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(r.content)
    d = pd.read_csv(p, low_memory=False)
    if "month" not in d.columns:
        return None
    d = d.sort_values("month")
    row: dict = {}
    any_ = False
    for v in variables:
        col, _unit, how = VARIABLES[v]
        vals = pd.to_numeric(d[col], errors="coerce") if col in d.columns else pd.Series([float("nan")] * len(d))
        vals = vals.where(vals > -7777)                   # NCEI's special values (-7777 trace, -8888 and -9999 missing) are not numbers
        months = dict(zip(d["month"].astype(int), vals, strict=True))
        for m in range(1, 13):
            row[f"{v}_{m:02d}"] = months.get(m)
        got = [x for x in (months.get(m) for m in range(1, 13)) if x is not None and x == x]
        if len(got) == 12:
            row[f"{v}_annual"] = round(sum(got) / 12.0, 2) if how == "mean" else round(sum(got), 2)
            any_ = True
        else:
            row[f"{v}_annual"] = None
    return row if any_ else None


def fetch(region, *, variables=("tavg", "tmax", "tmin", "prcp"), max_stations: int = MAX_STATIONS, out: Path | None = None, http=None,
          cache_dir=None) -> dict:
    import geopandas as gpd
    import pandas as pd
    bad = [v for v in variables if v not in VARIABLES]
    if bad:
        raise NormalsError(f"unknown variable(s) {', '.join(bad)}; they are {', '.join(VARIABLES)}", 2)
    sts, total = stations_in(region, http, cache_dir, max_stations)
    if not sts:
        raise NormalsError(f"no normals station lies inside {region.name} (the normals cover the United States and its territories)", 3, "MAP-REG-003")
    rows, skipped = [], 0
    for s in sts:
        n = station_normals(s["id"], variables, http, cache_dir)
        if n is None:
            skipped += 1
            continue
        rows.append({**s, **n})
    if not rows:
        raise NormalsError(f"none of the {len(sts)} nearest stations publishes {', '.join(variables)}", 3)
    df = pd.DataFrame(rows)
    g = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs="EPSG:4326")
    out = Path(out) if out else cache_root(cache_dir) / f"normals_{region.id}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    g.to_parquet(out, index=False)
    units = {f"{v}_*": VARIABLES[v][1] for v in variables}
    rec = {"entry": ENTRY, "region": region.name, "path": str(out), "stations": len(rows), "stations_in_region": total, "skipped": skipped,
           "variables": list(variables), "units": units, "attribution": ATTRIBUTION, "license": "public-domain",
           "retrieved_at": os.environ.get("GMAP_FIXED_DATE") or dt.date.today().isoformat(), "source": INVENTORY}
    out.with_suffix(".json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    notes = []
    if total > len(sts):
        notes.append(f"{total} stations lie in {region.name}; the {len(sts)} nearest its centre were read (--max-stations)")
    if skipped:
        notes.append(f"{skipped} station(s) publish none of {', '.join(variables)} and are left out")
    return {"ok": True, **rec, "notes": notes, "columns": [c for c in g.columns if c != "geometry"]}


def from_library(entry: dict, params: dict, *, refresh: bool = False, http=None, store=None) -> dict:
    """ref_fetch's tool route: params {region (text or admin codes), variables (comma list), max_stations}."""
    import service_tools as st
    txt = params.get("region") or (f"US-{params['state']}" if params.get("state") and len(str(params["state"])) == 2 else None)
    if not txt:
        raise NormalsError("NOAA normals are fetched for a region: region=US-WA, a place name, or --variant WA", 2)
    reg = st.region_of_text(txt)
    vs = tuple(v.strip() for v in str(params.get("variables") or "tavg,tmax,tmin,prcp").split(",") if v.strip())
    return fetch(reg, variables=vs, max_stations=int(params.get("max_stations") or MAX_STATIONS), http=http)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["fetch", "stations"])
    ap.add_argument("--region", required=True)
    ap.add_argument("--variables", default="tavg,tmax,tmin,prcp")
    ap.add_argument("--max-stations", type=int, default=MAX_STATIONS)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    import service_tools as st
    try:
        reg = st.region_of_text(a.region)
        if a.op == "stations":
            sts, total = stations_in(reg, max_stations=a.max_stations)
            res = {"ok": True, "region": reg.name, "stations_in_region": total, "stations": sts}
        else:
            res = fetch(reg, variables=tuple(v.strip() for v in a.variables.split(",") if v.strip()), max_stations=a.max_stations,
                        out=Path(a.out) if a.out else None)
    except NormalsError as ex:
        print(json.dumps({"ok": False, "error": str(ex)}), file=sys.stderr)
        return ex.code
    except Exception as ex:  # noqa: BLE001 - a region the gazetteer does not know, or a service failure
        print(json.dumps({"ok": False, "error": str(ex)[:300]}), file=sys.stderr)
        return getattr(ex, "code", 2) if isinstance(getattr(ex, "code", None), int) else 2
    print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
