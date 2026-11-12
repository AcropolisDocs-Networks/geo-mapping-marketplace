#!/usr/bin/env python3
"""raster_fetch: the population rasters a map or an op names by library id, fetched for a window and cached (M12 integration).

    import raster_fetch as rf
    r = rf.raster_for("ref.statistic.world.population.ghsl", (-112.3, 33.3, -111.8, 33.7))
    r["path"]      # a local GeoTIFF (the GHSL tile, or a mosaic of up to four tiles; or the WorldPop country file)
    r["record"]    # entry, urls, bytes, sha256, retrieved_at, cached, attribution (CC BY 4.0), the tiles or the country

Two entries are known (their download patterns are in the library entries):
  ref.statistic.world.population.ghsl       GHS-POP R2023A epoch 2020, 100 m, Mollweide (ESRI:54009), one zip per 1,000 km tile. The tile grid starts at
                                            x = -18,041,000 m, y = 9,000,000 m; row r and column c cover x0 + (c - 1) * 1e6 .. + 1e6, y0 - r * 1e6 .. + 1e6
                                            (checked against tile R5_C9 on 2026-11-05: -10,041,000 .. -9,041,000, 4,000,000 .. 5,000,000).
                                            A window over two to four tiles is merged into one GeoTIFF; more than four is refused (use a smaller window).
  ref.statistic.world.population.worldpop   WorldPop constrained 2020, 100 m, EPSG:4326, one file per country (ISO3 from the bundled Natural Earth countries
                                            at the window's centre). A window over two countries takes the centre's country and says so (GHSL is the one grid).

Rules: every request goes through ogc_http.SafeHTTP (public hosts, GET, the plugin's User-Agent, at most 200 MB a file: the client's ceiling, so the
WorldPop file for the United States, 494 MB, is refused with the GHSL alternative named). Files are kept under `<workdir>/fetch/rasters/<entry>/` with a
JSON record beside each; a second call for the same tile or country reads nothing from the network. GMAP_OFFLINE=1 refuses a fetch that is not cached.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402
import ogc_http as oh  # noqa: E402
import raster_io as rio  # noqa: E402

GHSL = "ref.statistic.world.population.ghsl"
WORLDPOP = "ref.statistic.world.population.worldpop"
ENTRIES = (GHSL, WORLDPOP)
GHSL_X0, GHSL_Y0, GHSL_TILE_M = -18_041_000.0, 9_000_000.0, 1_000_000.0
GHSL_MAX_TILES = 4
MAX_BYTES = 200_000_000                  # SafeHTTP's ceiling (CEILING["max_bytes"])


class RasterFetchError(rio.RasterError):
    """A refusal or failure with a message safe to show."""


def offline() -> bool:
    return os.environ.get("GMAP_OFFLINE") == "1"


def today() -> str:
    return os.environ.get("GMAP_FIXED_DATE") or dt.date.today().isoformat()


def cache_root(cache_dir=None) -> Path:
    return (Path(cache_dir) if cache_dir else gc.workdir(create=True)) / "fetch" / "rasters"


def library_entry(entry_id: str) -> dict:
    import ref_library as rl
    for e in rl.all_entries("all"):
        if e.get("id") == entry_id:
            return e
    raise RasterFetchError(f"the reference library has no entry '{entry_id}'")


def known(entry_id: str) -> bool:
    return entry_id in ENTRIES


# --------------------------------------------------------------------------------------------------------------- GHSL tiles
def ghsl_tiles(bounds_wgs84) -> list[tuple[int, int]]:
    """(row, col) of the 1,000 km Mollweide tiles a lon/lat window touches (its corners and edge midpoints, projected)."""
    from pyproj import Transformer
    x0, y0, x1, y1 = (float(v) for v in bounds_wgs84)
    t = Transformer.from_crs("EPSG:4326", "ESRI:54009", always_xy=True)
    lons = [x0, (x0 + x1) / 2, x1]
    lats = [y0, (y0 + y1) / 2, y1]
    out = set()
    for lo in lons:
        for la in lats:
            x, y = t.transform(lo, la)
            out.add((int((GHSL_Y0 - y) // GHSL_TILE_M) + 1, int((x - GHSL_X0) // GHSL_TILE_M) + 1))
    for r in range(min(o[0] for o in out), max(o[0] for o in out) + 1):           # every tile between the extremes
        for c in range(min(o[1] for o in out), max(o[1] for o in out) + 1):
            out.add((r, c))
    return sorted(out)


def ghsl_url(entry: dict, row: int, col: int) -> str:
    tpl = (entry.get("delivery") or {}).get("url_template") or ""
    if "{row}" not in tpl or "{col}" not in tpl:
        raise RasterFetchError(f"library entry {entry.get('id')} has no tile url_template naming the row and the column")
    return tpl.replace("{row}", str(int(row))).replace("{col}", str(int(col)))


# --------------------------------------------------------------------------------------------------------------- WorldPop country
def worldpop_iso3(bounds_wgs84) -> tuple[str, list[str]]:
    """(ISO3 of the country at the window's centre, every country the window touches) from the bundled Natural Earth countries."""
    import geopandas as gpd
    from shapely.geometry import Point, box
    g = gpd.read_parquet(gc.plugin_root() / "assets" / "basemaps" / "ne_110m_countries.parquet")
    g = g.to_crs("EPSG:4326") if g.crs is not None and not g.crs.equals("EPSG:4326") else g
    col = next((c for c in ("ADM0_A3", "ISO_A3", "SOV_A3") if c in g.columns), None)
    if col is None:
        raise RasterFetchError("the bundled countries layer has no ISO3 column")
    x0, y0, x1, y1 = bounds_wgs84
    c = Point((x0 + x1) / 2, (y0 + y1) / 2)
    hit = g[g.geometry.contains(c)]
    if not len(hit):
        hit = g.iloc[[int(g.geometry.distance(c).values.argmin())]]
    touch = sorted(set(str(v) for v in g[g.geometry.intersects(box(x0, y0, x1, y1))][col]))
    return str(hit[col].iloc[0]), touch


def worldpop_url(entry: dict, iso3: str) -> str:
    tpl = (entry.get("delivery") or {}).get("url_template") or ""
    if "{ISO3}" not in tpl:
        raise RasterFetchError(f"library entry {entry.get('id')} has no url_template naming the country code")
    return tpl.replace("{ISO3}", iso3.upper()).replace("{iso3}", iso3.lower())


# --------------------------------------------------------------------------------------------------------------- download
def _http(http=None):
    return http or oh.SafeHTTP(limits={"timeout_s": 120, "max_bytes": MAX_BYTES})


def _download(url: str, dest: Path, http, *, zipped: bool) -> dict:
    """GET url (through SafeHTTP) into dest; a zip is opened and its one GeoTIFF kept. -> {url, bytes, sha256, member?}."""
    if offline():
        raise RasterFetchError(f"offline (GMAP_OFFLINE=1): {dest.name} is not in the cache and nothing is fetched")
    try:
        r = http.get(url, None, expect="any", max_bytes=MAX_BYTES)
    except oh.ServiceError as e:
        msg = oh.untrusted(e, 200)
        if "larger than" in msg:
            raise RasterFetchError(f"{Path(url).name} is larger than the {MAX_BYTES // 1_000_000} MB a fetch may take; use the GHSL tiles "
                                   f"({GHSL}) for a large country, or download the file and give its path") from None
        raise RasterFetchError(f"the raster could not be fetched: {msg}") from None
    body = r.content
    dest.parent.mkdir(parents=True, exist_ok=True)
    rec = {"url": oh.redact_url(r.url), "bytes": len(body), "sha256_download": r.sha256}
    if zipped:
        if body[:4] != b"PK\x03\x04":
            raise RasterFetchError(f"{Path(url).name} is not a zip file (the server sent {oh.untrusted(r.content_type, 40) or 'something else'})")
        z = zipfile.ZipFile(io.BytesIO(body))
        tifs = [n for n in z.namelist() if n.lower().endswith((".tif", ".tiff")) and not n.startswith(("/", "..")) and ".." not in n]
        if not tifs:
            raise RasterFetchError(f"{Path(url).name} holds no GeoTIFF")
        with z.open(tifs[0]) as src, open(dest, "wb") as out:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        rec["member"] = tifs[0]
    else:
        if body[:4] not in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
            raise RasterFetchError(f"{Path(url).name} is not a GeoTIFF")
        dest.write_bytes(body)
    rec["sha256"] = _sha(dest)
    return rec


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cached(dest: Path) -> dict | None:
    rp = dest.with_suffix(".json")
    if dest.is_file() and rp.is_file():
        try:
            rec = json.loads(rp.read_text())
        except ValueError:
            return None
        if rec.get("sha256") == _sha(dest):
            return rec
    return None


def _one(url: str, dest: Path, http, zipped: bool) -> tuple[dict, bool]:
    rec = _cached(dest)
    if rec:
        return rec, True
    rec = _download(url, dest, http, zipped=zipped)
    rec["retrieved_at"] = today()
    dest.with_suffix(".json").write_text(json.dumps(rec, indent=2))
    return rec, False


# --------------------------------------------------------------------------------------------------------------- the one call
def raster_for(entry_id: str, bounds_wgs84, *, cache_dir=None, http=None, variant: str | None = None) -> dict:
    """A local GeoTIFF of the library raster covering the window. -> {path, record, findings, entry}."""
    if entry_id not in ENTRIES:
        raise RasterFetchError(f"'{entry_id}' is not a raster this fetcher knows ({', '.join(ENTRIES)}); give a file path instead")
    e = library_entry(entry_id)
    if e.get("status") in ("blocked", "retired", "draft"):
        raise RasterFetchError(f"library entry '{entry_id}' is {e.get('status')}")
    root = cache_root(cache_dir) / entry_id
    h = _http(http)
    lic = e.get("license") or {}
    findings: list[dict] = []
    fetched_from_cache = []
    if entry_id == GHSL:
        tiles = ghsl_tiles(bounds_wgs84)
        if len(tiles) > GHSL_MAX_TILES:
            raise RasterFetchError(f"the window touches {len(tiles)} GHSL tiles (1,000 km each); at most {GHSL_MAX_TILES} are fetched for one map. Use a smaller window")
        recs, paths = [], []
        for row, col in tiles:
            dest = root / f"R{row}_C{col}.tif"
            try:
                r, cached = _one(ghsl_url(e, row, col), dest, h, zipped=True)
            except RasterFetchError as ex:
                if len(tiles) > 1 and "404" in str(ex):                     # an all-sea tile is not published; its neighbours carry the land
                    findings.append({"id": "MAP-TER-005", "severity": "INFO", "detail": f"R{row}_C{col}",
                                     "message": f"GHSL tile R{row}_C{col} is not published (open sea); the window is read from the other tile(s)"})
                    continue
                raise
            recs.append({"tile": f"R{row}_C{col}", **r, "cached": cached})
            paths.append(dest)
            fetched_from_cache.append(cached)
        if not paths:
            raise RasterFetchError("no GHSL tile covers this window")
        tiles = [(int(x["tile"][1:].split("_C")[0]), int(x["tile"].split("_C")[1])) for x in recs]
        if len(paths) == 1:
            path = paths[0]
        else:
            path = root / ("mosaic_" + "_".join(f"R{r}C{c}" for r, c in tiles) + ".tif")
            if not path.is_file():
                _mosaic(paths, path)
        rec = {"entry": entry_id, "tiles": [x["tile"] for x in recs], "files": recs}
    else:
        iso3, touch = worldpop_iso3(bounds_wgs84)
        if len(touch) > 1:
            findings.append({"id": "MAP-TER-005", "severity": "INFO", "detail": touch,
                             "message": f"the window touches {', '.join(touch)}; WorldPop is one file per country, so {iso3} (the country at the window's centre) is used. "
                                        f"GHSL ({GHSL}) is one grid across borders"})
        url = worldpop_url(e, iso3)
        if variant == "unadj" or variant == "UNadj":
            url = url.replace("_ppp_2020_constrained", "_ppp_2020_UNadj_constrained")
        path = root / Path(url).name
        r, cached = _one(url, path, h, zipped=False)
        fetched_from_cache.append(cached)
        rec = {"entry": entry_id, "country": iso3, "files": [{**r, "cached": cached}]}
    rec.update({"retrieved_at": today() if not all(fetched_from_cache) else (rec["files"][0].get("retrieved_at") or today()),
                "cached": all(fetched_from_cache), "attribution": lic.get("attribution_text"), "license": lic.get("class"),
                "request_count": sum(0 if f.get("cached") else 1 for f in rec["files"]), "request_bytes": sum(0 if f.get("cached") else int(f.get("bytes", 0)) for f in rec["files"]),
                "sha256": _sha(path), "path": str(path), "bounds_wgs84": [float(v) for v in bounds_wgs84]})
    return {"path": str(path), "record": rec, "findings": findings, "entry": e}


def _mosaic(paths: list[Path], out: Path) -> None:
    import rasterio
    from rasterio.merge import merge
    srcs = [rasterio.open(p) for p in paths]
    try:
        arr, t = merge(srcs)
        prof = srcs[0].profile.copy()
        prof.update({"height": arr.shape[1], "width": arr.shape[2], "transform": t, "compress": "deflate", "tiled": True, "blockxsize": 256, "blockysize": 256, "BIGTIFF": "IF_SAFER"})
        with rasterio.open(out, "w", **prof) as dst:
            dst.write(arr)
    finally:
        for s in srcs:
            s.close()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Fetch a population raster (GHSL tile or WorldPop country) for a lon/lat window")
    ap.add_argument("entry", choices=list(ENTRIES))
    ap.add_argument("--bounds", required=True, help="lon0,lat0,lon1,lat1")
    a = ap.parse_args(argv)
    try:
        r = raster_for(a.entry, tuple(float(v) for v in a.bounds.split(",")))
    except rio.RasterError as e:
        print(str(e), file=sys.stderr)
        return gc.EXIT_RUNTIME
    print(json.dumps({"path": r["path"], "record": r["record"], "findings": r["findings"]}, indent=2, default=str))
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
