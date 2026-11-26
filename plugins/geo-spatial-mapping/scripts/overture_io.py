#!/usr/bin/env python3
"""overture_io: read one window of an Overture Maps GeoParquet theme by HTTPS range requests (1.5.0, R3-GND).

    overture_io.py files --release 2026-10-21.0 --type land          the release's files for a type (an S3 listing, read-only)
    overture_io.py read  --release R --type water --bbox=W,S,E,N --out F.parquet [--cap-mb 300]
    overture_io.py read  --release R --theme buildings --type building --bbox=W,S,E,N --out F.parquet     1.16.0: buildings (height, floors, name)
    overture_io.py read  --release R --theme transportation --type segment ...  |  --theme base --type infrastructure (piers, bridges)

Rules (spec R3-GND):
- Every byte goes through SafeHTTP (`ogc_http.SafeHTTP.get` with a `Range` header): public-address checks, the capped response, the audit. No
  DuckDB httpfs and no S3FileSystem.
- The host is the Overture bucket's HTTPS endpoint (BUCKET_HOST) and the path is `release/<pinned release>/theme=<theme>/type=<type>/`; any other
  host or path is refused before a request is sent.
- A `pyarrow` file object makes byte-range GETs; each file's footer is read once and kept (`_footers/` in the cache), so a later window reads only
  the row groups whose `bbox` column statistics meet it.
- The bytes read for a window are capped (`cap_bytes`, default 300 MB): over it the read stops with CapExceeded (MAP-GND-003) and nothing is kept.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import threading
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

BUCKET_HOST = "overturemaps-us-west-2.s3.us-west-2.amazonaws.com"
BASE_URL = f"https://{BUCKET_HOST}/"
DEFAULT_CAP = 300_000_000
BLOCK = 1 << 20                     # read-ahead within one range request
RELEASE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
TYPES = {"base": ("land", "water", "land_cover", "bathymetry", "infrastructure"), "divisions": ("division_area", "division", "division_boundary"),
         "buildings": ("building", "building_part"), "transportation": ("segment", "connector"),
         "places": ("place",), "addresses": ("address",)}          # 5.4.0 (open data portals P4.1): places, addresses, divisions, boundaries, connectors
COLUMNS = ["id", "subtype", "class", "bbox", "geometry"]
# 1.16.0 (Day 15 issues 7 and 15): the columns a type is read with beyond COLUMNS; `names` comes back as `name` (its primary name). Day 15 read
# buildings, roads and piers through DuckDB on S3 (proxy-injected AWS keys broke it) with a "bbox inside the window" filter that dropped the long
# segments crossing its edge; this reader is anonymous HTTPS and keeps every feature whose bbox meets the window.
TYPE_COLUMNS = {"building": ["names", "height", "min_height", "num_floors", "min_floor", "roof_shape", "has_parts"],
                "building_part": ["names", "height", "min_height", "num_floors", "min_floor", "building_id"],
                "segment": ["names", "subtype", "class"], "infrastructure": ["names", "height"], "water": ["names"], "land": ["names"],
                # 5.4.0: a place comes back with name, category (its taxonomy's primary), brand, address, locality, region, country, postcode and website;
                # e-mail addresses, telephone numbers and social accounts are not read
                "place": ["names", "basic_category", "taxonomy", "confidence", "operating_status", "brand", "addresses", "websites"],
                "address": ["country", "postcode", "postal_city", "street", "number", "unit"],
                "division": ["names", "country", "region", "admin_level", "population", "wikidata", "parent_division_id"],
                "division_boundary": ["country", "region", "admin_level", "is_land", "is_territorial", "is_disputed"],
                "division_area": ["names", "country", "region", "admin_level", "division_id", "is_land", "is_territorial"],
                "connector": []}


class CapExceeded(Exception):
    """The window needs more bytes than the cap allows (MAP-GND-003)."""


class Refused(Exception):
    pass


def _check(release: str, theme: str, typ: str) -> str:
    if not RELEASE_RE.match(str(release)):
        raise Refused(f"'{release}' is not an Overture release name (YYYY-MM-DD.N)")
    if typ not in TYPES.get(theme, ()):
        raise Refused(f"theme={theme}/type={typ} is not a type the plugin reads")
    return f"release/{release}/theme={theme}/type={typ}/"


def _url_ok(url: str, prefix: str, host: str = BUCKET_HOST) -> None:
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or (u.hostname or "").lower() != host or u.port not in (None, 443):
        raise Refused(f"this reader reads only https://{host}/")
    path = urllib.parse.unquote(u.path.lstrip("/"))
    if path and not path.startswith(prefix):
        raise Refused(f"this reader reads only {prefix}")


def _http(http=None):
    if http is not None:
        return http
    import ogc_http
    return ogc_http.SafeHTTP(limits={"timeout_s": 60, "max_bytes": 200_000_000})


_BUDGET_LOCK = threading.Lock()
FOOTER_THREADS = 6            # 4.2.0 (Day 25 #11): a type's file footers are read side by side, once, then kept
FILE_BBOX_INDEX = "{typ}.bbox.json"


def file_bbox(md) -> list | None:
    """The box that holds every row group of a file, from its footer's bbox statistics; None when a group has none."""
    cols = _bbox_cols(md)
    if cols is None or not md.num_row_groups:
        return None
    lo_x = lo_y = float("inf")
    hi_x = hi_y = float("-inf")
    for i in range(md.num_row_groups):
        rg = md.row_group(i)
        st = {k: rg.column(c).statistics for k, c in cols.items()}
        if any(s is None or not s.has_min_max for s in st.values()):
            return None
        lo_x, hi_x = min(lo_x, st["xmin"].min), max(hi_x, st["xmax"].max)
        lo_y, hi_y = min(lo_y, st["ymin"].min), max(hi_y, st["ymax"].max)
    return [float(lo_x), float(lo_y), float(hi_x), float(hi_y)]


def _index_read(footer_dir: Path | None, typ: str) -> dict:
    if footer_dir is None:
        return {}
    try:
        return json.loads((footer_dir / FILE_BBOX_INDEX.format(typ=typ)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _index_write(footer_dir: Path | None, typ: str, index: dict) -> None:
    if footer_dir is None:
        return
    try:
        footer_dir.mkdir(parents=True, exist_ok=True)
        (footer_dir / FILE_BBOX_INDEX.format(typ=typ)).write_text(json.dumps(index), encoding="utf-8")
    except OSError:
        pass


class Budget:
    def __init__(self, cap: int):
        self.cap, self.used, self.requests = int(cap), 0, 0

    def take(self, n: int) -> None:
        with _BUDGET_LOCK:                                   # 4.2.0: footers are read by several threads
            self.used += n
            self.requests += 1
        if self.used > self.cap:
            raise CapExceeded(f"the window needs more than {self.cap / 1e6:,.0f} MB of this source's data (read {self.used / 1e6:,.1f} MB so far)")


class RangeFile(io.RawIOBase):
    """A read-only, seekable file over HTTPS range GETs, for pyarrow. Each request reads at least BLOCK bytes ahead."""

    def __init__(self, http, url: str, size: int, budget: Budget, prefix: str, host: str = BUCKET_HOST):
        _url_ok(url, prefix, host)                              # 5.4.0: `host` lets the generic GeoParquet reader (window_read.py) use this file object
        self.http, self.url, self.size, self.budget, self.pos = http, url, int(size), budget, 0
        self._buf_start, self._buf = 0, b""

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def _fetch(self, a: int, b: int) -> bytes:
        r = self.http.get(self.url, headers={"Range": f"bytes={a}-{b - 1}", "Accept-Encoding": "identity"}, max_bytes=b - a + 1024)
        if r.status not in (200, 206):
            raise OSError(f"HTTP {r.status} for a range of {self.url.rsplit('/', 1)[-1]}")
        data = r.content if r.status == 206 else r.content[a:b]
        self.budget.take(len(data))
        return data

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        if n == 0:
            return b""
        a, b = self.pos, self.pos + n
        if not (self._buf_start <= a and b <= self._buf_start + len(self._buf)):
            end = min(self.size, max(b, a + BLOCK))
            self._buf_start, self._buf = a, self._fetch(a, end)
        out = self._buf[a - self._buf_start:b - self._buf_start]
        self.pos += len(out)
        return out

    def readinto(self, b):
        d = self.read(len(b))
        b[:len(d)] = d
        return len(d)


def list_files(release: str, typ: str, *, theme: str = "base", http=None) -> list[dict]:
    """[{key, size}] of a release's files for a type, from the bucket's own S3 listing (ListObjectsV2, a read-only GET)."""
    prefix = _check(release, theme, typ)
    h = _http(http)
    out, token = [], None
    while True:
        params = {"list-type": "2", "prefix": prefix}
        if token:
            params["continuation-token"] = token
        r = h.get(BASE_URL, params=params, max_bytes=5_000_000)
        if r.status != 200:
            raise OSError(f"the Overture listing answered HTTP {r.status}")
        s = r.content.decode("utf-8", "replace")
        for blk in re.findall(r"<Contents>(.*?)</Contents>", s, re.S):
            k = re.search(r"<Key>([^<]*)</Key>", blk)
            z = re.search(r"<Size>(\d+)</Size>", blk)
            if k and z and k.group(1).endswith(".parquet") and k.group(1).startswith(prefix):
                out.append({"key": k.group(1), "size": int(z.group(1))})
        m = re.search(r"<NextContinuationToken>([^<]*)</NextContinuationToken>", s)
        if re.search(r"<IsTruncated>true</IsTruncated>", s) and m:
            token = m.group(1)
            continue
        return out


def _bbox_cols(md) -> dict | None:
    names = [md.schema.column(i).path for i in range(md.num_columns)]
    want = {k: f"bbox.{k}" for k in ("xmin", "xmax", "ymin", "ymax")}
    if not all(v in names for v in want.values()):
        return None
    return {k: names.index(v) for k, v in want.items()}


def row_groups_in(md, bbox) -> list[int]:
    """The row groups whose bbox statistics meet the window (all of them when the file has no statistics)."""
    cols = _bbox_cols(md)
    W, S, E, N = bbox
    if cols is None:
        return list(range(md.num_row_groups))
    out = []
    for i in range(md.num_row_groups):
        rg = md.row_group(i)
        st = {k: rg.column(c).statistics for k, c in cols.items()}
        if any(s is None or not s.has_min_max for s in st.values()):
            out.append(i)
            continue
        if st["xmin"].min <= E and st["xmax"].max >= W and st["ymin"].min <= N and st["ymax"].max >= S:
            out.append(i)
    return out


def read_window(release: str, typ: str, bbox, *, theme: str = "base", http=None, cap_bytes: int = DEFAULT_CAP, footer_dir: Path | None = None,
                columns=None, files: list[dict] | None = None):
    """The features of one type whose bbox meets the window, as a GeoDataFrame (EPSG:4326), and what the read cost.
    -> (gdf, {files, row_groups, bytes, requests})."""
    import geopandas as gpd
    import pyarrow as pa
    import pyarrow.parquet as pq
    import shapely
    prefix = _check(release, theme, typ)
    h = _http(http)
    budget = Budget(cap_bytes)
    files = files if files is not None else list_files(release, typ, theme=theme, http=h)
    W, S, E, N = (float(v) for v in bbox)
    tables, n_rg, n_files = [], 0, 0
    # 4.2.0 (Day 25 #11: a buildings read took nine minutes, most of it opening every file of the type one after another to read its footer).
    # 1. A file whose box, kept from an earlier read (`<type>.bbox.json` beside the footers), misses the window is not opened at all.
    # 2. The footers still to read are fetched by FOOTER_THREADS threads, each with its own connection, and kept, so they are read once.
    index = _index_read(footer_dir, typ)
    known = len(index)

    def cache_of(f):
        return (footer_dir / f"{typ}-{Path(f['key']).name}.meta") if footer_dir else None      # by type: file names can repeat across types

    def misses(f) -> bool:
        b = index.get(Path(f["key"]).name)
        return bool(b) and not (b[0] <= E and b[2] >= W and b[1] <= N and b[3] >= S)

    todo = [f for f in files if not misses(f)]
    skipped_by_index = len(files) - len(todo)

    def footer(f, hh):
        cached = cache_of(f)
        if cached is not None and cached.exists():
            try:
                return pq.read_metadata(cached)
            except Exception:  # noqa: BLE001 - a damaged footer copy is read again
                pass
        pf_ = pq.ParquetFile(pa.PythonFile(RangeFile(hh, BASE_URL + f["key"], f["size"], budget, prefix), mode="r"))
        if cached is not None:
            cached.parent.mkdir(parents=True, exist_ok=True)
            pf_.metadata.write_metadata_file(str(cached))
        return pf_.metadata

    need = [f for f in todo if not (cache_of(f) is not None and cache_of(f).exists())]
    metas: dict = {}
    if http is None and len(need) > 2:                     # the plugin's own connections: one per thread (a caller's http is used in order)
        import concurrent.futures as cf
        local = threading.local()

        def one(f):
            if not hasattr(local, "h"):
                local.h = _http(None)
            return f["key"], footer(f, local.h)
        with cf.ThreadPoolExecutor(max_workers=FOOTER_THREADS) as ex:
            for key, md_ in ex.map(one, need):
                metas[key] = md_
    for f in todo:
        url = BASE_URL + f["key"]
        md = metas.get(f["key"]) or footer(f, h)
        name = Path(f["key"]).name
        if name not in index:
            fb = file_bbox(md)
            if fb:
                index[name] = fb
        rgs = row_groups_in(md, (W, S, E, N))
        if not rgs:
            continue
        src = RangeFile(h, url, f["size"], budget, prefix)
        pf = pq.ParquetFile(pa.PythonFile(src, mode="r"), metadata=md)
        n_files += 1
        n_rg += len(rgs)
        names = pf.schema_arrow.names
        cols = [c for c in (columns or (COLUMNS + TYPE_COLUMNS.get(typ, []))) if c in names]
        t = pf.read_row_groups(rgs, columns=cols)
        if "bbox" in t.column_names:
            import pyarrow.compute as pc
            bb = t.column("bbox")
            m = pc.and_(pc.and_(pc.less_equal(pc.struct_field(bb, "xmin"), E), pc.greater_equal(pc.struct_field(bb, "xmax"), W)),
                        pc.and_(pc.less_equal(pc.struct_field(bb, "ymin"), N), pc.greater_equal(pc.struct_field(bb, "ymax"), S)))
            t = t.filter(m)
        if t.num_rows:
            tables.append(t)
    if len(index) != known:
        _index_write(footer_dir, typ, index)
    cost = {"files": n_files, "row_groups": n_rg, "bytes": budget.used, "requests": budget.requests,
            "files_of_type": len(files), "files_skipped_by_index": skipped_by_index, "footers_read": len(need)}
    if not tables:
        return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:4326"), cost
    t = pa.concat_tables(tables, promote_options="default")
    geom = shapely.from_wkb(t.column("geometry").to_numpy(zero_copy_only=False))
    data = flat_columns(t)
    return gpd.GeoDataFrame(data, geometry=list(geom), crs="EPSG:4326"), cost


def flat_columns(t) -> dict:
    """A table's columns as plain values: the nested ones Overture uses are read down to the one value a map needs. `names` gives `name` (its
    primary name, 1.16.0); 5.4.0: `taxonomy` gives `category`, `brand` its name, the first of `addresses` gives address, locality, region,
    country and postcode, the first of `websites` gives `website`. A nested column this does not know is left out."""
    import pyarrow as pa
    import pyarrow.compute as pc
    names = set(t.column_names)
    data: dict = {}

    def field(col: str, *path):
        a = t.column(col)
        try:
            for part in path:
                a = pc.struct_field(a, part)
            return a.to_pylist()
        except (pa.ArrowInvalid, KeyError, pa.ArrowNotImplementedError):
            return [None] * t.num_rows

    for c in t.column_names:
        if c in ("geometry", "bbox"):
            continue
        typ = t.schema.field(c).type
        if c == "names":
            data["name"] = field("names", "primary")
        elif c == "taxonomy":
            cat = field("taxonomy", "primary")
            basic = t.column("basic_category").to_pylist() if "basic_category" in names else [None] * t.num_rows
            data["category"] = [a or b for a, b in zip(cat, basic)]
        elif c == "basic_category":
            if "taxonomy" not in names:
                data["category"] = t.column(c).to_pylist()
        elif c == "brand":
            data["brand"] = field("brand", "names", "primary")
        elif c == "addresses":
            first = [(x[0] if x else None) for x in t.column(c).to_pylist()]
            for out, key in (("address", "freeform"), ("locality", "locality"), ("region", "region"), ("country", "country"), ("postcode", "postcode")):
                if out not in data:
                    data[out] = [(a or {}).get(key) if isinstance(a, dict) else None for a in first]
        elif c == "websites":
            data["website"] = [(x[0] if x else None) for x in t.column(c).to_pylist()]
        elif pa.types.is_nested(typ):
            continue
        else:
            data[c] = t.column(c).to_pylist()
    return data


# ------------------------------------------------------------------------------------------------ detailed ground: one z8 quadkey tile a variant
GROUND_PROVIDER = "global-overture"
TILE_ZOOM = 8
LAND_CLASSES = {"land", "island", "islet"}
WATER_SUBTYPES = {"lake", "pond", "reservoir", "river", "canal", "water", "lagoon"}
WATER_CLASS_SKIP = {"swimming_pool", "reflecting_pool", "fountain", "wastewater"}
QK_RE = re.compile(r"^[0-3]{1,12}$")


def tile_of(lon: float, lat: float, z: int = TILE_ZOOM) -> tuple[int, int]:
    import math
    n = 2 ** z
    lat = max(-85.05112878, min(85.05112878, float(lat)))
    x = int((float(lon) + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def quadkey(x: int, y: int, z: int = TILE_ZOOM) -> str:
    out = []
    for i in range(z, 0, -1):
        m = 1 << (i - 1)
        out.append(str((1 if x & m else 0) + (2 if y & m else 0)))
    return "".join(out)


def qk_tile(qk: str) -> tuple[int, int, int]:
    x = y = 0
    for i, c in enumerate(qk):
        m = 1 << (len(qk) - i - 1)
        d = int(c)
        x |= m if d & 1 else 0
        y |= m if d & 2 else 0
    return x, y, len(qk)


def qk_bounds(qk: str) -> tuple[float, float, float, float]:
    import math
    x, y, z = qk_tile(qk)
    n = 2 ** z
    lat = lambda yy: math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))  # noqa: E731
    return x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y)


def tiles_for(bbox, z: int = TILE_ZOOM) -> list[str]:
    W, S, E, N = (float(v) for v in bbox)
    x0, y0 = tile_of(W, N, z)
    x1, y1 = tile_of(E, S, z)
    return [quadkey(x, y, z) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def ground_dir(release: str) -> Path:
    if not RELEASE_RE.match(str(release)):
        raise Refused(f"'{release}' is not an Overture release name (YYYY-MM-DD.N)")
    return gc.outputs_dir() / "cache" / "ground" / GROUND_PROVIDER / str(release)


def tile_path(release: str, qk: str) -> Path:
    return ground_dir(release) / f"{qk}.parquet"


def find_tile(release: str, qk: str) -> Path | None:
    """1.14.0: a cached ground tile wherever a prepare left it (gmap_common.cache_roots), or None. Writing stays in tile_path()."""
    p = tile_path(release, qk)
    if p.is_file():
        return p
    return gc.find_cached("cache", "ground", GROUND_PROVIDER, str(release), f"{qk}.parquet")


def ground_features(land, water):
    """The land (polygons of class land, island, islet) and water areas (polygons, no pools or the sea) of a read, as one frame with `kind`."""
    import geopandas as gpd
    import pandas as pd
    import shapely
    parts = []
    if land is not None and len(land):
        sub = land["subtype"] if "subtype" in land.columns else pd.Series(["land"] * len(land), index=land.index)
        cls = land["class"] if "class" in land.columns else pd.Series(["land"] * len(land), index=land.index)
        m = (sub == "land") & cls.isin(LAND_CLASSES) & land.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
        parts.append(gpd.GeoDataFrame({"kind": "land", "class": cls[m].values}, geometry=land.geometry[m].values, crs="EPSG:4326"))
    if water is not None and len(water):
        sub = water["subtype"] if "subtype" in water.columns else pd.Series(["water"] * len(water), index=water.index)
        cls = water["class"] if "class" in water.columns else pd.Series(["water"] * len(water), index=water.index)
        m = sub.isin(WATER_SUBTYPES) & ~cls.isin(WATER_CLASS_SKIP) & water.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
        parts.append(gpd.GeoDataFrame({"kind": "water", "class": cls[m].values}, geometry=water.geometry[m].values, crs="EPSG:4326"))
    parts = [p for p in parts if len(p)]
    if not parts:
        return gpd.GeoDataFrame({"kind": [], "class": []}, geometry=[], crs="EPSG:4326")
    g = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs="EPSG:4326")
    g["geometry"] = shapely.make_valid(g.geometry.values)
    return g[~g.geometry.is_empty]


def fetch_tile(qk: str, release: str, *, http=None, refresh: bool = False, cap_bytes: int = DEFAULT_CAP, files: dict | None = None) -> dict:
    """Read one z8 tile's land and water into the ground cache (all or nothing). -> {quadkey, path, cached, bytes, features}."""
    import datetime as dt
    import shapely
    if not QK_RE.match(str(qk)) or len(qk) != TILE_ZOOM:
        raise Refused(f"'{qk}' is not a zoom-{TILE_ZOOM} quadkey (eight digits 0 to 3)")
    out = tile_path(release, qk)
    if out.exists() and not refresh:
        return {"quadkey": qk, "path": str(out), "cached": True}
    h = _http(http)
    bb = qk_bounds(qk)
    foot = ground_dir(release) / "_footers"
    used = 0
    reads = {}
    for typ in ("land", "water"):
        fl = (files or {}).get(typ)
        g, cost = read_window(release, typ, bb, http=h, cap_bytes=max(1, cap_bytes - used), footer_dir=foot, files=fl)
        used += cost["bytes"]
        reads[typ] = (g, cost)
    g = ground_features(reads["land"][0], reads["water"][0])
    if len(g):
        g["geometry"] = shapely.make_valid(shapely.clip_by_rect(g.geometry.values, *bb))
        g = g[~g.geometry.is_empty & g.geometry.geom_type.isin(["Polygon", "MultiPolygon", "GeometryCollection"])]
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    g.to_parquet(tmp)
    rec = {"provider": GROUND_PROVIDER, "release": release, "quadkey": qk, "bounds": [round(v, 6) for v in bb], "bytes_read": used,
           "land": int((g["kind"] == "land").sum()) if len(g) else 0, "water": int((g["kind"] == "water").sum()) if len(g) else 0,
           "read": {k: v[1] for k, v in reads.items()}, "read_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    tmp.replace(out)
    out.with_suffix(".json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    return {"quadkey": qk, "path": str(out), "cached": False, "bytes": used, "features": len(g)}


def from_library(entry: dict, params: dict, *, refresh: bool = False, http=None, store=None) -> dict:
    """ref_fetch's hook (`delivery.tool: overture_io`): the variant is one z8 quadkey; the release is the entry's pinned one."""
    qk = str((params or {}).get("state") or (params or {}).get("quadkey") or "")
    release = str((entry.get("delivery") or {}).get("release") or "")
    cap = int(float((entry.get("delivery") or {}).get("cap_mb", DEFAULT_CAP / 1e6)) * 1e6)
    r = fetch_tile(qk, release, http=http, refresh=refresh, cap_bytes=cap)
    return {"id": entry["id"], "rev": entry.get("rev"), "variant": qk, "release": release, **r}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=("files", "read"))
    ap.add_argument("--release", required=True)
    ap.add_argument("--type", required=True)
    ap.add_argument("--theme", default="base")
    ap.add_argument("--bbox")
    ap.add_argument("--out")
    ap.add_argument("--cap-mb", type=float, default=DEFAULT_CAP / 1e6)
    ap.add_argument("--no-cache", action="store_true", help="4.2.0: do not keep the files' footers and boxes")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "files":
            print(json.dumps(list_files(a.release, a.type, theme=a.theme), indent=2))
            return gc.EXIT_OK
        # 4.2.0: the footers and each file's box are kept beside the ground cache, so the second window of a type opens only the files it needs
        foot = None if a.no_cache else ground_dir(a.release) / "_footers" / a.theme
        g, cost = read_window(a.release, a.type, [float(v) for v in a.bbox.split(",")], theme=a.theme, cap_bytes=int(a.cap_mb * 1e6), footer_dir=foot)
        if a.out:
            g.to_parquet(a.out)
        print(json.dumps({"features": len(g), **cost}, indent=2))
        return gc.EXIT_OK
    except (Refused, CapExceeded) as ex:
        print(f"overture_io: {ex}", file=sys.stderr)
        return gc.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
