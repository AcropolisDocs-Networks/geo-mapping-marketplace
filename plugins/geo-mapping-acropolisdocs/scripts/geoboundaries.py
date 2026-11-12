#!/usr/bin/env python3
"""geoboundaries: one country's ADM1 or ADM2 boundaries from the geoBoundaries gbOpen release (1.5.0, R3-LIB).

    ref_fetch.py fetch ref.boundary.world.country.geoboundaries --variant nga --level adm1
    geoboundaries.py fetch NGA ADM1 [--refresh]          the same, from this script
    geoboundaries.py info  NGA ADM2                      the API record (source, licence, year, unit count); nothing is downloaded

Rules:
- Only gbOpen. The API record's `boundaryLicense` is read for every country and level, and only CC BY 4.0 or a more open licence (CC0, public
  domain, the ODC Public Domain Dedication) is used; anything else is refused with the licence named, before the boundaries are downloaded.
- Every request goes through SafeHTTP. The GeoJSON link the API gives is a github.com file stored with Git LFS; where github.com refuses the
  request (the cloud workspace), the same file is read from media.githubusercontent.com, GitHub's host for LFS content.
- The boundaries are cached as GeoParquet in the library cache with a record beside them: source, licence, year, the commit in the link, SHA-256.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

API = "https://www.geoboundaries.org/api/current/gbOpen/{iso3}/{adm}/"
LEVELS = ("ADM0", "ADM1", "ADM2", "ADM3", "ADM4")
OPEN = re.compile(r"(?i)\bCC0\b|public domain|creative commons attribution 4\.0|\bCC[ -]BY[ -]4\.0\b|open data commons public domain|\bPDDL\b")
GH_RAW = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/raw/([0-9a-f]{6,40})/(.+)$")
MAX_BYTES = 150_000_000


class GBError(Exception):
    def __init__(self, message: str, code: int = gc.EXIT_SERVICE, rule: str | None = None):
        super().__init__(message)
        self.code, self.rule = code, rule


def _http(http=None):
    if http is not None:
        return http
    import ogc_http
    return ogc_http.SafeHTTP(limits={"timeout_s": 90, "max_bytes": MAX_BYTES})


def _norm(iso3: str, level: str) -> tuple[str, str]:
    iso3 = str(iso3 or "").strip().upper()
    lv = str(level or "ADM1").strip().upper().replace("ADMIN", "ADM")
    if not re.match(r"^[A-Z]{3}$", iso3):
        raise GBError(f"'{iso3}' is not an ISO 3166-1 alpha-3 code (NGA, GRC, JPN)", gc.EXIT_USAGE)
    if lv not in LEVELS:
        raise GBError(f"level '{level}' is not one of {', '.join(LEVELS)}", gc.EXIT_USAGE)
    return iso3, lv


def info(iso3: str, level: str = "ADM1", *, http=None) -> dict:
    iso3, lv = _norm(iso3, level)
    r = _http(http).get(API.format(iso3=iso3, adm=lv), max_bytes=2_000_000)
    if r.status != 200:
        raise GBError(f"geoBoundaries has no gbOpen {lv} for {iso3} (HTTP {r.status})")
    d = json.loads(r.content)
    d = d[0] if isinstance(d, list) else d
    keep = ("boundaryID", "boundaryName", "boundaryISO", "boundaryType", "boundaryCanonical", "boundaryYearRepresented", "boundarySource",
            "boundaryLicense", "licenseDetail", "licenseSource", "boundarySourceURL", "admUnitCount", "gjDownloadURL", "simplifiedGeometryGeoJSON")
    return {k: d.get(k) for k in keep if k in d}


def licence_ok(rec: dict) -> bool:
    return bool(OPEN.search(str(rec.get("boundaryLicense") or "")))


def media_url(url: str) -> str | None:
    m = GH_RAW.match(url or "")
    return f"https://media.githubusercontent.com/media/{m.group(1)}/{m.group(2)}/{m.group(3)}/{m.group(4)}" if m else None


def _download(url: str, http) -> tuple[bytes, str]:
    """The GeoJSON bytes and the URL they came from: github.com first, GitHub's LFS host when github.com refuses or answers an LFS pointer."""
    tried = []
    for u in [url, media_url(url)]:
        if not u:
            continue
        try:
            r = http.get(u, max_bytes=MAX_BYTES)
        except Exception as ex:  # noqa: BLE001 - a refused host is tried on the other
            tried.append(f"{u.split('/')[2]}: {type(ex).__name__}")
            continue
        if r.status == 200 and r.content[:200].lstrip().startswith(b"{"):
            return r.content, u
        tried.append(f"{u.split('/')[2]}: HTTP {r.status}" + (" (a Git LFS pointer)" if r.content.startswith(b"version https://git-lfs") else ""))
    raise GBError("the geoBoundaries file could not be read (" + "; ".join(tried) + ")")


def cache_path(entry_id: str, iso3: str, lv: str) -> Path:
    import ref_fetch as rf
    return rf.cache_dir() / rf.cache_name(entry_id, f"{iso3.lower()}-{lv.lower()}")


def fetch(iso3: str, level: str = "ADM1", *, entry_id: str = "ref.boundary.world.country.geoboundaries", http=None, refresh: bool = False,
          simplified: bool = False) -> dict:
    import geopandas as gpd
    iso3, lv = _norm(iso3, level)
    out = cache_path(entry_id, iso3, lv)
    rec_p = out.with_suffix(".json")
    if out.is_file() and rec_p.is_file() and not refresh:
        rec = json.loads(rec_p.read_text())
        return {"id": entry_id, "variant": iso3.lower(), "level": lv, "path": str(out), "record": str(rec_p), "cached": True,
                "licence": rec.get("licence"), "features": rec.get("features")}
    h = _http(http)
    meta = info(iso3, lv, http=h)
    if not licence_ok(meta):
        raise GBError(f"geoBoundaries {iso3} {lv} is under '{meta.get('boundaryLicense')}', which is not CC BY 4.0 or more open; it is not used "
                      "(gbOpen, licence read from the API record)", gc.EXIT_REFUSED)
    url = meta.get("simplifiedGeometryGeoJSON" if simplified else "gjDownloadURL") or meta.get("gjDownloadURL")
    data, got_from = _download(url, h)
    tmp = out.with_suffix(".geojson.part")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(data)
    try:
        g = gpd.read_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    g = g.set_crs("EPSG:4326", allow_override=True) if g.crs is None else g.to_crs("EPSG:4326")
    g.to_parquet(out)
    commit = (GH_RAW.match(url or "") or [None, None, None, None])[3] if GH_RAW.match(url or "") else None
    rec = {"source": "geoBoundaries gbOpen", "iso3": iso3, "level": lv, "api": API.format(iso3=iso3, adm=lv), "url": url, "read_from": got_from,
           "commit": commit, "licence": meta.get("boundaryLicense"), "licence_source": meta.get("licenseSource"), "boundary_source": meta.get("boundarySource"),
           "year": meta.get("boundaryYearRepresented"), "units_stated": meta.get("admUnitCount"), "features": len(g), "bytes": len(data),
           "sha256": hashlib.sha256(data).hexdigest(), "retrieved_at": dt.date.today().isoformat(),
           "attribution_text": f"Boundaries: geoBoundaries (geoboundaries.org), {meta.get('boundarySource')}, {meta.get('boundaryLicense')}."}
    rec_p.write_text(json.dumps(rec, indent=2))
    return {"id": entry_id, "variant": iso3.lower(), "level": lv, "path": str(out), "record": str(rec_p), "cached": False,
            "licence": rec["licence"], "features": len(g)}


def from_library(entry: dict, params: dict, *, refresh: bool = False, http=None, store=None) -> dict:
    """ref_fetch's hook (`delivery.tool: geoboundaries`): variant = the ISO 3166-1 alpha-3 code (nga), level = adm1 or adm2."""
    p = params or {}
    v = str(p.get("state") or p.get("iso3") or "")
    if len(v) == 2:                                  # an alpha-2 code: the table's alpha-3
        import geo_locale as gl
        v = (gl.country(v) or {}).get("iso3") or v
    return fetch(v, p.get("level") or "ADM1", entry_id=entry["id"], http=http, refresh=refresh)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("fetch", "info"))
    ap.add_argument("iso3")
    ap.add_argument("level", nargs="?", default="ADM1")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args(argv)
    try:
        r = info(a.iso3, a.level) if a.cmd == "info" else fetch(a.iso3, a.level, refresh=a.refresh)
        print(json.dumps(r, indent=2))
        return gc.EXIT_OK
    except GBError as ex:
        print(f"geoboundaries: {ex}", file=sys.stderr)
        return ex.code


if __name__ == "__main__":
    sys.exit(main())
