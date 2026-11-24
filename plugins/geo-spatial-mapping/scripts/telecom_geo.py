#!/usr/bin/env python3
"""telecom_geo: the telecom conventions the coverage, market and program maps share (spec 7.4 telecom-geo, 7.2).

    telecom_geo.py sites TABLE [--sheet S] [--out sites.csv]        read and check a site/sector list
    telecom_geo.py wedges TABLE --out wedges.gpkg [--radius 1500]   sector wedges as display geometry
    telecom_geo.py status TABLE --column Status                     how a tracker's status words map to the five program statuses

Site lists. Columns are found by common names (site_id, lat/latitude, lon/long/longitude, azimuth/az, beamwidth/bw, band, tech/rat, site_type, sector). The checks
are MAP-TEL-010 to 019. Nothing is geocoded: positions come from the table, never from an address lookup.

Wedges are DISPLAY geometry: a pie slice of `radius_m` and `beamwidth` degrees pointing along the azimuth. They show where an antenna points and roughly how far
we chose to draw it. They are not propagation, not predicted coverage, and not a service area (MAP-TEL-020 says so on the map's methods note).

Statuses. Tracker words (Green, On Track, Amber, Yellow, At Risk, Red, Late, Blocked, Complete, Done, Not Started, TBD...) map to five program statuses, each with a
brand status color and its own shape, so color is never the only signal (MAP-A11Y-003).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

MAX_WEDGES = 20_000
DEFAULT_BEAMWIDTH = 65.0
DEFAULT_RADIUS_M = 1500.0
ARC_STEP_DEG = 5.0

ALIASES = {
    "site_id": ("site_id", "siteid", "site", "site_name", "sitename", "cell_site", "cell_id", "cellid", "enb", "gnb", "enodeb", "gnodeb", "id", "name"),
    "lat": ("lat", "latitude", "lat_dd", "y", "ycoord", "site_lat", "latitude_dd"),
    "lon": ("lon", "long", "lng", "longitude", "lon_dd", "x", "xcoord", "site_lon", "longitude_dd"),
    "azimuth": ("azimuth", "az", "azm", "azim", "bearing", "sector_azimuth", "antenna_azimuth", "direction"),
    "beamwidth": ("beamwidth", "beam_width", "bw", "hbw", "hbeamwidth", "horizontal_beamwidth", "h_beamwidth"),
    "band": ("band", "freq_band", "frequency_band", "carrier", "spectrum", "band_mhz", "frequency"),
    "tech": ("tech", "technology", "rat", "radio", "access_technology", "generation"),
    "site_type": ("site_type", "sitetype", "type", "structure", "structure_type", "site_class"),
    "sector": ("sector", "sector_id", "sectorid", "cell", "sector_name"),
}

# Order bands and technologies the way the industry reads them (low band to high, older to newer), so a band keeps the same color from map to map.
BAND_ORDER = ["600", "700", "850", "AWS", "1700", "PCS", "1900", "2100", "2500", "n41", "C-band", "3700", "CBRS", "3500", "mmWave", "n260", "n261"]
TECH_ORDER = ["2G", "GSM", "3G", "UMTS", "LTE", "4G", "5G NSA", "5G NR", "5G SA", "5G"]
SITE_TYPE_ORDER = ["Macro", "Rooftop", "Tower", "Small cell", "DAS", "COW", "Other"]
SITE_SHAPES = {"Macro": "triangle", "Rooftop": "square", "Tower": "triangle", "Small cell": "circle", "DAS": "diamond", "COW": "cross", "Other": "circle"}

STATUSES = [
    # key, label, brand color token, shape
    ("complete", "Complete", "@brand.status.success", "circle"),
    ("on-track", "On track", "@brand.status.success", "square"),
    ("at-risk", "At risk", "@brand.status.warning", "triangle"),
    ("delayed", "Delayed or blocked", "@brand.status.danger", "diamond"),
    ("not-started", "Not started", "@brand.no_data", "cross"),
]
STATUS_WORDS = {
    "complete": ("complete", "completed", "done", "finished", "delivered", "closed", "live", "in service", "on air", "launched", "go live", "accepted", "ready"),
    "on-track": ("on track", "ontrack", "green", "g", "in progress", "underway", "on schedule", "ok", "active", "in flight", "started"),
    "at-risk": ("at risk", "atrisk", "amber", "yellow", "a", "y", "watch", "caution", "slipping", "behind", "minor delay", "risk"),
    "delayed": ("delayed", "late", "red", "r", "blocked", "critical", "overdue", "off track", "offtrack", "stalled", "on hold", "hold", "cancelled", "canceled"),
    "not-started": ("not started", "notstarted", "planned", "tbd", "to do", "todo", "pending", "future", "not begun", "n/a", "na", "unknown", "grey", "gray", "backlog", "queued"),
}


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def _norm_col(c) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_")


def detect_columns(columns) -> dict:
    """{canonical: actual column} using ALIASES; the first alias that is present wins, so 'site_id' beats 'name'."""
    norm = {_norm_col(c): c for c in columns}
    out = {}
    for key, names in ALIASES.items():
        for n in names:
            if n in norm and norm[n] not in out.values():
                out[key] = norm[n]
                break
    return out


def canonical_order(values, kind: str = "band") -> list[str]:
    """The categories present, in industry order first and the rest alphabetically, so colors stay put from map to map."""
    ref = {"band": BAND_ORDER, "tech": TECH_ORDER, "site_type": SITE_TYPE_ORDER}.get(kind, [])
    present = []
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() not in ("nan", "none") and s not in present:
            present.append(s)
    low = {r.lower(): i for i, r in enumerate(ref)}
    return sorted(present, key=lambda s: (low.get(s.lower(), len(ref)), s.lower()))


def _num(v):
    try:
        f = float(str(v).strip().replace(",", ""))
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


class SiteListError(ValueError):
    """The site list cannot be used at all; `fid` is the rule that says why."""
    def __init__(self, message: str, fid: str = "MAP-TEL-010"):
        super().__init__(message)
        self.fid = fid


TABLE_SUFFIXES = (".xlsx", ".xlsm", ".csv", ".tsv", ".txt")


def _geo_suffix(p: Path) -> bool:
    """Whether a site list is a geodata file (anything geo_io reads: GeoPackage, GeoJSON, shapefile or its zip, KML/KMZ, GPX, TopoJSON,
    FlatGeobuf, GeoParquet) rather than a table. A .csv is a table here even though geo_io reads CSVs too, because a site list's columns
    (azimuth, band, sector) are read by name from the table and the coordinates go through the same checks either way."""
    import geo_io
    if p.suffix.lower() in TABLE_SUFFIXES:
        return False
    try:
        return geo_io.detect_format(p) != "csv"
    except geo_io.GeoIOError:
        return False


def ingest_sites(path, sheet: str | None = None, *, columns: dict | None = None, layer: str | None = None):
    """(DataFrame with lon, lat, site_id, azimuth, beamwidth, band, tech, site_type where found, findings, report). Raises ValueError if the table cannot be used.

    A geodata file gives its positions from its geometry: a point as it is; a polygon or a line (a site footprint, a tower parcel, a
    rooftop outline) as its representative point, with a finding that says so (MAP-TEL-010 INFO), rather than failing on `.x`."""
    import pandas as pd
    import table_join as tj
    p = Path(path)
    fs = []
    if _geo_suffix(p):
        import geo_io
        g = geo_io.read(p, layer=layer).gdf
        if g.crs is None:
            raise SiteListError(f"'{p.name}' states no CRS, so its positions cannot be placed; convert it with a CRS first (geo-convert --crs-assume)", "MAP-TEL-010")
        g = g.to_crs("EPSG:4326")
        geoms = g.geometry
        empty = geoms.isna() | geoms.is_empty
        kinds = geoms[~empty].geom_type
        not_point = ~empty & (kinds.reindex(geoms.index) != "Point")
        if not_point.any():
            reps = geoms[not_point].representative_point()
            geoms = geoms.copy()
            geoms[not_point] = reps
            by_kind = kinds[not_point.reindex(kinds.index, fill_value=False)].value_counts().to_dict()
            fs.append(finding("MAP-TEL-010", "INFO", f"{int(not_point.sum())} feature(s) are not points ({', '.join(f'{v} {k}' for k, v in by_kind.items())}); "
                              "each site is placed at a point inside its shape (a representative point, not a centroid, so it is never outside a crescent)", by_kind))
        df = pd.DataFrame(g.drop(columns=g.geometry.name))
        xs = [None if (gm is None or gm.is_empty) else float(gm.x) for gm in geoms]
        ys = [None if (gm is None or gm.is_empty) else float(gm.y) for gm in geoms]
        df["lon"], df["lat"] = xs, ys                        # replaces any lon/lat attribute: the geometry is the position
    else:
        df = tj.read_table(p, sheet)
    det = dict(detect_columns(df.columns))
    if _geo_suffix(p):
        det["lat"], det["lon"] = "lat", "lon"               # the geometry is the position, whatever the attributes are called
    if columns:
        det.update({k: v for k, v in columns.items() if v})
    if "lat" not in det or "lon" not in det:
        raise ValueError(f"'{p.name}' needs latitude and longitude columns (found: {', '.join(map(str, df.columns))}); say which columns hold them")
    out = pd.DataFrame(index=df.index)
    out["lat"] = df[det["lat"]].map(_num)
    out["lon"] = df[det["lon"]].map(_num)
    for k in ("site_id", "sector", "band", "tech", "site_type"):
        if k in det:
            out[k] = df[det[k]].map(lambda v: None if v is None or str(v).strip().lower() in ("", "nan", "none") else str(v).strip())
    for k in ("azimuth", "beamwidth"):
        if k in det:
            out[k] = df[det[k]].map(_num)
    n0 = len(out)
    bad_num = out["lat"].isna() | out["lon"].isna()
    if bad_num.any():
        fs.append(finding("MAP-TEL-010", "WARN", f"{int(bad_num.sum())} of {n0} row(s) have no usable latitude or longitude and were left out", [int(i) + 2 for i in out.index[bad_num][:20]]))
    out = out[~bad_num]
    zero = (out["lat"].abs() < 1e-6) & (out["lon"].abs() < 1e-6)
    if zero.any():
        fs.append(finding("MAP-TEL-011", "WARN", f"{int(zero.sum())} row(s) sit at 0,0 (a blank turned into zero) and were left out"))
        out = out[~zero]
    swapped = (out["lat"].abs() > 90) & (out["lon"].abs() <= 90)
    if swapped.any():
        if swapped.sum() == len(out):
            raise SiteListError(f"every row has a latitude beyond 90: latitude and longitude look swapped in '{p.name}'; swap the two columns or say which is which with columns=", "MAP-TEL-012")
        fs.append(finding("MAP-TEL-012", "WARN", f"{int(swapped.sum())} row(s) have a latitude beyond 90: latitude and longitude look swapped", [int(i) + 2 for i in out.index[swapped][:20]]))
        out = out[~swapped]
    outside = (out["lat"].abs() > 90) | (out["lon"].abs() > 180)
    if outside.any():
        fs.append(finding("MAP-TEL-013", "WARN", f"{int(outside.sum())} row(s) have coordinates outside the world and were left out", [int(i) + 2 for i in out.index[outside][:20]]))
        out = out[~outside]
    if len(out):
        mlon, mlat = float(out["lon"].median()), float(out["lat"].median())
        if 66 <= mlon <= 125 and 24 <= mlat <= 50:
            # a latitude like North America's with a longitude of 66 to 125 east: the sites are in China and Central Asia, or the minus sign was dropped
            fs.append(finding("MAP-TEL-014", "WARN", f"the median site is at {mlat:.2f}, {mlon:.2f}: if these are North American sites, the longitude is missing its minus sign"))
    key = [c for c in ("site_id", "sector") if c in out.columns]
    if key == ["site_id"] and "azimuth" in out.columns:
        key = ["site_id", "azimuth"]                        # several sectors per site is normal; the same site and direction twice is not
    if key and (len(key) > 1 or "azimuth" not in out.columns):
        dup = out.duplicated(subset=key, keep=False)
        if dup.any():
            fs.append(finding("MAP-TEL-015", "WARN", f"{int(dup.sum())} row(s) repeat the same {' + '.join(key)}; each is drawn", [int(i) + 2 for i in out.index[dup][:20]]))
    if "azimuth" in out.columns:
        bad = out["azimuth"].notna() & ((out["azimuth"] < 0) | (out["azimuth"] > 360))
        if bad.any():
            fs.append(finding("MAP-TEL-016", "WARN", f"{int(bad.sum())} azimuth(s) are outside 0 to 360 degrees and were treated as missing", [int(i) + 2 for i in out.index[bad][:20]]))
            out.loc[bad, "azimuth"] = None
        out["azimuth"] = out["azimuth"] % 360
        miss = out["azimuth"].isna()
        if miss.any():
            fs.append(finding("MAP-TEL-017", "INFO", f"{int(miss.sum())} row(s) have no azimuth; they are drawn as omni-directional circles if wedges are made"))
    if "beamwidth" in out.columns:
        bad = out["beamwidth"].notna() & ((out["beamwidth"] <= 0) | (out["beamwidth"] > 360))
        if bad.any():
            fs.append(finding("MAP-TEL-018", "WARN", f"{int(bad.sum())} beamwidth(s) are outside 1 to 360 degrees and use the default of {DEFAULT_BEAMWIDTH:g}"))
            out.loc[bad, "beamwidth"] = None
    if len(out) == 0:
        raise SiteListError(f"'{p.name}' has no usable site rows", "MAP-TEL-010")
    out = out.reset_index(drop=True)
    rep = {"rows_in": int(n0), "rows_used": int(len(out)), "columns": {k: v for k, v in det.items()}, "sectors": int(out["azimuth"].notna().sum()) if "azimuth" in out.columns else 0,
           "bands": canonical_order(out["band"], "band") if "band" in out.columns else [], "tech": canonical_order(out["tech"], "tech") if "tech" in out.columns else [],
           "site_types": canonical_order(out["site_type"], "site_type") if "site_type" in out.columns else []}
    return out, fs, rep


def wedge_polygon(lon: float, lat: float, azimuth: float | None, beamwidth: float, radius_m: float, geod=None):
    """A pie-slice polygon (lon/lat), or a circle when there is no azimuth or the beam is 360 degrees."""
    from pyproj import Geod
    import shapely
    geod = geod or Geod(ellps="WGS84")
    if azimuth is None or beamwidth >= 359.9:
        a0, a1, apex = 0.0, 360.0, False
    else:
        a0, a1, apex = azimuth - beamwidth / 2.0, azimuth + beamwidth / 2.0, True
    n = max(2, int(math.ceil((a1 - a0) / ARC_STEP_DEG)) + 1)
    pts = []
    for i in range(n):
        a = a0 + (a1 - a0) * i / (n - 1)
        x, y, _ = geod.fwd(lon, lat, a, radius_m)
        pts.append((x, y))
    if apex:
        pts = [(lon, lat)] + pts + [(lon, lat)]
    elif pts[0] != pts[-1]:
        pts.append(pts[0])
    return shapely.Polygon(pts)


def make_wedges(df, *, radius_m: float = DEFAULT_RADIUS_M, beamwidth: float | None = None, radius_field: str | None = None):
    """GeoDataFrame (EPSG:4326) with one wedge per row of a site table from ingest_sites. Returns (gdf, findings)."""
    import geopandas as gpd
    from pyproj import Geod
    fs = []
    if not (50 <= radius_m <= 50_000):
        raise ValueError(f"wedge radius must be between 50 m and 50 km; got {radius_m:g} m")
    if len(df) > MAX_WEDGES:
        raise ValueError(f"{len(df):,} sectors is more than one map takes ({MAX_WEDGES:,}); filter to the market first")
    geod = Geod(ellps="WGS84")
    bw_default = float(beamwidth) if beamwidth else DEFAULT_BEAMWIDTH
    geoms, bws = [], []
    for _, r in df.iterrows():
        az = r.get("azimuth") if "azimuth" in df.columns else None
        az = None if az is None or (isinstance(az, float) and math.isnan(az)) else float(az)
        bw = r.get("beamwidth") if "beamwidth" in df.columns else None
        bw = bw_default if bw is None or (isinstance(bw, float) and math.isnan(bw)) else float(bw)
        rad = float(r[radius_field]) if radius_field and radius_field in df.columns and r[radius_field] == r[radius_field] else float(radius_m)
        if not (50 <= rad <= 50_000):
            raise ValueError(f"a sector's radius must be between 50 m and 50 km; '{radius_field}' gives {rad:g} m")
        geoms.append(wedge_polygon(float(r["lon"]), float(r["lat"]), az, bw, rad, geod))
        bws.append(bw)
    g = gpd.GeoDataFrame(df.drop(columns=["lat", "lon"]).copy(), geometry=geoms, crs="EPSG:4326")
    g["beamwidth"] = bws if "beamwidth" not in g.columns else g["beamwidth"].fillna(bw_default)
    return g, fs


WEDGE_NOTE = ("Sector wedges are drawn for display: each shows the direction an antenna points and a fixed drawing radius; they are not predicted coverage, "
              "signal strength or a service area.")


def normalize_status(value) -> str | None:
    """A tracker status word mapped to a program status key, or None when it is not recognised."""
    if value is None:
        return None
    s = re.sub(r"[^a-z0-9/ ]+", " ", str(value).strip().lower())
    s = re.sub(r"\s+", " ", s).strip()
    if not s or s == "nan":
        return None
    for key, words in STATUS_WORDS.items():
        if s in words:
            return key
    for key, words in STATUS_WORDS.items():
        if any(len(w) > 3 and (s.startswith(w) or w in s.split()) for w in words):
            return key
    m = re.match(r"^(\d{1,3})\s*%$", s)
    if m:
        p = int(m.group(1))
        return "complete" if p >= 100 else ("not-started" if p == 0 else "on-track")
    return None


def status_spec() -> dict:
    """The classification/palette block for a status layer: order, brand colors and shapes (never color alone)."""
    return {"order": [s[1] for s in STATUSES], "colors": [s[2] for s in STATUSES], "shapes": [s[3] for s in STATUSES]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="op", required=True)
    s = sub.add_parser("sites")
    s.add_argument("table")
    s.add_argument("--sheet")
    s.add_argument("--out")
    w = sub.add_parser("wedges")
    w.add_argument("table")
    w.add_argument("--sheet")
    w.add_argument("--out", required=True)
    w.add_argument("--radius", type=float, default=DEFAULT_RADIUS_M)
    w.add_argument("--beamwidth", type=float)
    w.add_argument("--overwrite", action="store_true")
    t = sub.add_parser("status")
    t.add_argument("table")
    t.add_argument("--sheet")
    t.add_argument("--column", required=True)
    a = ap.parse_args(argv)
    try:
        if a.op == "status":
            import table_join as tj
            df = tj.read_table(a.table, a.sheet)
            if a.column not in df.columns:
                print(f"no column '{a.column}'; columns: {', '.join(df.columns)}", file=sys.stderr)
                return 2
            res = {}
            for v in df[a.column]:
                k = normalize_status(v)
                res.setdefault(str(v), k)
            print(json.dumps({"mapping": res, "unrecognised": [v for v, k in res.items() if k is None]}, indent=2))
            return 0
        df, fs, rep = ingest_sites(a.table, a.sheet)
        if a.op == "sites":
            if a.out:
                df.to_csv(a.out, index=False)
            print(json.dumps({"report": rep, "findings": fs}, indent=2))
            return 3 if any(f["severity"] == "BLOCK" for f in fs) else 0
        import geo_io
        g, wf = make_wedges(df, radius_m=a.radius, beamwidth=a.beamwidth)
        geo_io.write(g, a.out, overwrite=a.overwrite)
        print(json.dumps({"written": a.out, "wedges": int(len(g)), "note": WEDGE_NOTE, "findings": fs + wf}, indent=2))
        return 0
    except ValueError as ex:
        print(f"telecom_geo: {ex}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
