#!/usr/bin/env python3
"""fcc_uls: the FCC's weekly ULS and Antenna Structure Registration downloads as map layers (1.15.0).

    fcc_uls.py fetch r_tower [--out DIR]                       download one complete file from data.fcc.gov (r_tower, l_cell, l_micro, ...)
    fcc_uls.py towers r_tower.zip OUT.gpkg [--state AZ] [--status C] [--bbox=W,S,E,N]
    fcc_uls.py locations l_cell.zip OUT.gpkg [--state AZ] [--active] [--bbox=W,S,E,N]

The FCC publishes each radio service and the antenna structures as a weekly zip of pipe-delimited tables with no header row
(data.fcc.gov/download/pub/uls/complete/). The host refused automated requests until 2026-11; on 2026-11-14 it answered from the build
session (HTTP 200, byte ranges offered), so the plugin can fetch a file itself, through the guarded client (public host, capped size, audit).

Antenna structures (r_tower.zip): CO holds the coordinates (degrees, minutes and seconds with N/S and E/W; coordinate type T is the
structure), RA the registration: status, structure type, the height of the structure, the ground elevation, the overall height above
ground and above mean sea level (metres), the address. One point per registration, from its T coordinate.

Licences (l_*.zip): LO holds each licensed location (DMS like CO, the ground elevation, the antenna structure registration number and
heights), HD the licence (call sign, licence status, radio service). One point per location.

The coordinates are as filed, NAD83 (EPSG:4269). Public domain; the credit is "Source: FCC Universal Licensing System" or "Source: FCC
Antenna Structure Registration".
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

HOST = "data.fcc.gov"
BASE = f"https://{HOST}/download/pub/uls/complete/"
FILE_RE = re.compile(r"^[alr]_[A-Za-z0-9]+$|^(?:owner|vs_market|vs_site|d_tower|a_tower)$")
MAX_BYTES = 500 * 1024 * 1024
CRS = "EPSG:4269"
# the columns used, by position in each header-less table (the ULS public access definitions; checked against the 2026-11-08 files)
CO = {"file_number": 2, "registration_number": 3, "usi": 4, "coordinate_type": 5, "lat": (6, 7, 8, 9), "lon": (11, 12, 13, 14)}
RA = {"usi": 4, "status": 8, "date_constructed": 12, "date_dismantled": 13, "address": 23, "city": 24, "state": 25, "county_code": 26,
      "height_of_structure_m": 28, "ground_elevation_m": 29, "overall_height_agl_m": 30, "overall_height_amsl_m": 31, "structure_type": 32}
LO = {"usi": 1, "call_sign": 4, "location_number": 8, "address": 11, "city": 12, "county": 13, "state": 14, "ground_elevation_m": 18,
      "lat": (19, 20, 21, 22), "lon": (23, 24, 25, 26), "asr_number": 37, "support_height_m": 38, "overall_height_m": 39, "structure_type": 40}
HD = {"usi": 1, "call_sign": 4, "license_status": 5, "radio_service": 6}
STATUS = {"A": "approved", "C": "constructed", "D": "dismantled", "G": "granted", "I": "cancelled", "T": "terminated", "W": "withdrawn"}


class ULSError(ValueError):
    pass


def dms(d: str, m: str, s: str, hemi: str) -> float | None:
    try:
        v = float(d or 0) + float(m or 0) / 60.0 + float(s or 0) / 3600.0
    except ValueError:
        return None
    if not d:
        return None
    return -v if hemi.strip().upper() in ("S", "W") else v


def _rows(z: zipfile.ZipFile, name: str):
    """A table's rows as lists of strings (pipe-delimited, no header, CRLF), streamed."""
    member = next((n for n in z.namelist() if Path(n).name.upper() == name.upper()), None)
    if member is None:
        raise ULSError(f"the zip has no {name}; it holds {', '.join(sorted(Path(n).name for n in z.namelist())[:12])}")
    with z.open(member) as fh:
        for line in io.TextIOWrapper(fh, encoding="latin-1", newline=""):
            line = line.rstrip("\r\n")
            if line:
                yield line.split("|")


def _get(r: list, i: int) -> str:
    return r[i].strip() if i < len(r) else ""


def _num(v: str):
    try:
        return float(v) if v.strip() else None
    except ValueError:
        return None


def _frame(rows: list[dict], xs: list, ys: list, bbox=None):
    import geopandas as gpd
    import shapely
    g = gpd.GeoDataFrame(rows, geometry=shapely.points(xs, ys), crs=CRS)
    if bbox:
        g = g[g.geometry.intersects(shapely.box(*[float(v) for v in bbox]))]
    return g.reset_index(drop=True)


def towers(path, *, state: str | None = None, status: list[str] | None = None, bbox=None):
    """Antenna structure registrations from r_tower.zip as points. -> (GeoDataFrame in EPSG:4269, notes)."""
    with zipfile.ZipFile(path) as z:
        ra = {}
        for r in _rows(z, "RA.dat"):
            ra[_get(r, RA["usi"])] = r
        rows, xs, ys, skipped = [], [], [], 0
        for c in _rows(z, "CO.dat"):
            if _get(c, CO["coordinate_type"]).upper() not in ("T", ""):
                continue
            usi = _get(c, CO["usi"])
            a = ra.get(usi)
            if a is None:
                continue
            st = _get(a, RA["status"])
            if status and st not in status:
                continue
            if state and _get(a, RA["state"]).upper() != state.upper():
                continue
            lat, lon = dms(*[_get(c, i) for i in CO["lat"]]), dms(*[_get(c, i) for i in CO["lon"]])
            if lat is None or lon is None:
                skipped += 1
                continue
            row = {"registration_number": _get(c, CO["registration_number"]), "usi": usi, "file_number": _get(c, CO["file_number"]),
                   "status": STATUS.get(st, st), "structure_type": _get(a, RA["structure_type"])}
            for k in ("address", "city", "state", "county_code", "date_constructed", "date_dismantled"):
                row[k] = _get(a, RA[k]) or None
            for k in ("height_of_structure_m", "ground_elevation_m", "overall_height_agl_m", "overall_height_amsl_m"):
                row[k] = _num(_get(a, RA[k]))
            rows.append(row)
            xs.append(lon)
            ys.append(lat)
    notes = [f"{len(rows):,} antenna structures read" + (f"; {skipped} without usable coordinates left out" if skipped else "")]
    return _frame(rows, xs, ys, bbox), notes


def locations(path, *, state: str | None = None, active: bool = False, bbox=None):
    """Licensed locations from an l_*.zip as points, with the licence's call sign, status and radio service. -> (GeoDataFrame, notes)."""
    with zipfile.ZipFile(path) as z:
        hd = {}
        for r in _rows(z, "HD.dat"):
            hd[_get(r, HD["usi"])] = r
        rows, xs, ys, skipped = [], [], [], 0
        for r in _rows(z, "LO.dat"):
            h = hd.get(_get(r, LO["usi"]))
            ls = _get(h, HD["license_status"]) if h else ""
            if active and ls != "A":
                continue
            if state and _get(r, LO["state"]).upper() != state.upper():
                continue
            lat, lon = dms(*[_get(r, i) for i in LO["lat"]]), dms(*[_get(r, i) for i in LO["lon"]])
            if lat is None or lon is None:
                skipped += 1
                continue
            row = {k: (_get(r, LO[k]) or None) for k in ("usi", "call_sign", "location_number", "address", "city", "county", "state", "asr_number", "structure_type")}
            for k in ("ground_elevation_m", "support_height_m", "overall_height_m"):
                row[k] = _num(_get(r, LO[k]))
            row["license_status"] = ls or None
            row["radio_service"] = (_get(h, HD["radio_service"]) or None) if h else None
            rows.append(row)
            xs.append(lon)
            ys.append(lat)
    notes = [f"{len(rows):,} licensed locations read" + (f"; {skipped} without usable coordinates (areas, mobiles) left out" if skipped else "")]
    return _frame(rows, xs, ys, bbox), notes


def fetch(name: str, out: Path | None = None, *, http=None) -> dict:
    """One complete file from data.fcc.gov, through the guarded client (public host, the size capped at MAX_BYTES, the request audited)."""
    import hashlib
    import ogc_http as oh
    if not FILE_RE.match(name or ""):
        raise ULSError(f"'{name}' is not a ULS complete file name (r_tower, l_cell, l_micro, a_tower, ...)")
    out = Path(out) if out else gc.outputs_dir(True) / "cache" / "fcc-uls"
    out.mkdir(parents=True, exist_ok=True)
    http = http or oh.SafeHTTP(limits={"timeout_s": 600, "max_bytes": MAX_BYTES})
    r = http.get(BASE + f"{name}.zip", None, expect="any")
    if r.content[:2] != b"PK":
        raise ULSError("the answer is not a zip file")
    p = out / f"{name}.zip"
    p.write_bytes(r.content)
    return {"path": str(p), "bytes": len(r.content), "sha256": hashlib.sha256(r.content).hexdigest(), "url": BASE + f"{name}.zip",
            "retrieved_at": gc.today_iso() if hasattr(gc, "today_iso") else __import__("datetime").date.today().isoformat()}


def _bbox(s: str | None):
    return [float(v) for v in s.split(",")] if s else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch"); f.add_argument("name"); f.add_argument("--out")
    t = sub.add_parser("towers"); t.add_argument("zip"); t.add_argument("out"); t.add_argument("--state"); t.add_argument("--status", help="e.g. C,G")
    t.add_argument("--bbox")
    lo = sub.add_parser("locations"); lo.add_argument("zip"); lo.add_argument("out"); lo.add_argument("--state"); lo.add_argument("--active", action="store_true")
    lo.add_argument("--bbox")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "fetch":
            print(json.dumps(fetch(a.name, Path(a.out) if a.out else None), indent=2))
            return 0
        if a.cmd == "towers":
            g, notes = towers(a.zip, state=a.state, status=a.status.split(",") if a.status else None, bbox=_bbox(a.bbox))
        else:
            g, notes = locations(a.zip, state=a.state, active=a.active, bbox=_bbox(a.bbox))
        out = Path(a.out)
        g.to_file(out, driver="GPKG" if out.suffix.lower() == ".gpkg" else None)
        print(json.dumps({"out": str(out), "features": len(g), "crs": CRS, "notes": notes, "fields": [c for c in g.columns if c != "geometry"]}, indent=2))
    except (ULSError, zipfile.BadZipFile) as ex:
        print(f"fcc_uls: {ex}", file=sys.stderr)
        return gc.EXIT_USAGE
    return 0


if __name__ == "__main__":
    sys.exit(main())
