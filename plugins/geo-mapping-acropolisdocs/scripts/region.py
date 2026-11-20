#!/usr/bin/env python3
"""region: the place a map is about, resolved once (2.6.0 for the H3 complete grid; 3.0.0, spec global-regions G-01 to G-05).

    region: {admin: US-WA}                         an ISO 3166-1 country (KE) or ISO 3166-2 admin-1 area (US-WA, DE-BY, CA-BC)
    region: {admin: [US-WA, US-OR, CA-BC]}          several, across borders (G-04)
    region: {admin2: {in: US-ID, names: [Bonner, Boundary]}}    admin-2 areas inside one admin-1 area
    region: {place: "Idaho Panhandle"}              a named region from the gazetteer (data/regions.yaml, and the person's own)
    region: {file: area.gpkg}  {bbox: [W, S, E, N]}  {h3: [cells]}
    any of them with buffer_km: 10

    resolve(block, base) -> Region(geometry (lon/lat), name, definition, countries, admin1, partitions, sources, notes)
    region.py resolve --spec map.yaml | --place "Cascadia" | --admin US-WA,CA-BC     prints what a region resolves to (no map)

Where the shapes come from (G-02). Countries and admin-1 areas worldwide: Natural Earth 1:10m admin-1 (bundled, offline), the US states from
the Census cartographic boundaries (bundled). Admin-2: US counties from the Census cartographic boundaries (bundled); outside the US from
geoBoundaries ADM2 (CC BY 4.0, fetched once by the library's geoboundaries route) when the person allows the fetch. The region keeps the ISO
codes; `sources` names every boundary source used, for the provenance.

Partitions (G-11). The adapters split their fetches by the region's own partitions: `us_states` (the FCC BDC files are per state, the Census per
county), `countries` (ISO 3166-1 alpha-2) and `admin1` (ISO 3166-2), each the areas the region touches by more than a sliver.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gmap_common as gc  # noqa: E402

KEYS = ("admin", "admin2", "place", "file", "bbox", "h3")
ADMIN1 = "ne_10m_admin1_loc.parquet"
COUNTRIES = "ne_110m_countries.parquet"
US_STATES = "us_states_cb.parquet"
US_COUNTIES = "us_counties_cb.parquet"
INSET_DEG = 0.03                  # about 3 km: the region is shrunk this much before its partitions are read
SLIVER = 0.001                    # a partition counts when the region covers more than this share of it, or it covers this share of the region
CODE = re.compile(r"^[A-Z]{2}(?:-[A-Z0-9]{1,3})?$")
_CACHE: dict = {}


class RegionError(Exception):
    def __init__(self, message: str, code: int = gc.EXIT_USAGE, rule: str = "MAP-REG-001"):
        super().__init__(message)
        self.code = code
        self.rule = rule


@dataclass
class Region:
    geometry: object
    name: str
    definition: str
    countries: list = field(default_factory=list)
    admin1: list = field(default_factory=list)
    partitions: dict = field(default_factory=dict)
    sources: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    members: list = field(default_factory=list)

    @property
    def bounds(self) -> tuple:
        return tuple(float(v) for v in self.geometry.bounds)

    def gdf(self):
        import geopandas as gpd
        return gpd.GeoDataFrame({"name": [self.name], "definition": [self.definition]}, geometry=[self.geometry], crs="EPSG:4326")

    def record(self) -> dict:
        return {"name": self.name, "definition": self.definition, "countries": self.countries, "admin1": self.admin1,
                "partitions": self.partitions, "sources": self.sources, "bounds": [round(v, 5) for v in self.bounds], "members": self.members}


def _basemap(name: str):
    if name not in _CACHE:
        import geopandas as gpd
        _CACHE[name] = gpd.read_parquet(gc.plugin_root() / "assets" / "basemaps" / name)
    return _CACHE[name]


# ---------------------------------------------------------------------------------------------------------------- the gazetteer
def gazetteer(base: Path | None = None) -> list[dict]:
    """The shipped named regions and the person's own (<base>/.geo-mapping/regions.yaml); the person's win on a name clash."""
    rows = list(gc.load_yaml(gc.plugin_root() / "data" / "regions.yaml") or [])
    own: list = []
    for d in [base / gc.WORKDIR_NAME if base else None, gc.workdir()]:
        p = d / "regions.yaml" if d else None
        if p and p.is_file():
            try:
                own += [dict(r, origin="yours") for r in gc.load_yaml(p) or []]
            except Exception:  # noqa: BLE001
                pass
    names = {_norm(r["name"]) for r in own}
    return own + [r for r in rows if _norm(r["name"]) not in names]


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def find_place(name: str, base: Path | None = None) -> dict | None:
    want = _norm(name)
    for r in gazetteer(base):
        if want == _norm(r["name"]) or want in {_norm(a) for a in r.get("aliases") or []}:
            return r
    return None


def by_code(code: str, base: Path | None = None) -> dict | None:
    for r in gazetteer(base):
        if code.upper() in [str(c).upper() for c in r.get("codes") or []]:
            return r
    return None


# ---------------------------------------------------------------------------------------------------------------- shapes
def admin_shape(code: str, base: Path | None = None):
    """(geometry, source, name) for a country or admin-1 code, or a gazetteer code (GB-SCT)."""
    code = str(code).strip().upper()
    g = by_code(code, base)
    if g is not None:
        r = resolve({"place": g["name"]}, base)
        return r.geometry, "; ".join(r.sources), g["name"]
    if not CODE.match(code):
        raise RegionError(f"region admin code '{code}' is not ISO 3166 (KE, US-WA, DE-BY)")
    if code.startswith("US-") and len(code) == 5:
        cn, src_name = _finer_us_counties()
        if "library cache" in src_name:            # the finer counties, dissolved, when cached (Day 20 Washington: 37,652 cells)
            hit = cn[cn["STUSPS"] == code[3:]]
            if len(hit):
                st_name = _basemap(US_STATES)
                nm = st_name[st_name["STUSPS"] == code[3:]]["NAME"]
                return hit.to_crs("EPSG:4326").geometry.union_all(), src_name + ", dissolved to the state", (str(nm.iloc[0]) if len(nm) else code)
        st = _basemap(US_STATES)
        hit = st[st["STUSPS"] == code[3:]]
        if len(hit):
            return hit.geometry.union_all(), "US Census cartographic boundary states (bundled)", str(hit["NAME"].iloc[0])
    a1 = _basemap(ADMIN1)
    if "-" in code:
        hit = a1[a1["iso_3166_2"] == code]
        if not len(hit):
            raise RegionError(f"no admin-1 area {code} (ISO 3166-2) in Natural Earth's admin-1 table")
        return hit.geometry.union_all(), "Natural Earth 1:10m admin-1 (bundled)", str(hit["name"].iloc[0])
    hit = a1[a1["iso_a2"] == code]
    if len(hit):
        c = _basemap(COUNTRIES)
        nm = c[c["ISO_A2_EH"] == code]
        return hit.geometry.union_all(), "Natural Earth 1:10m admin-1, as one country (bundled)", (str(nm["NAME"].iloc[0]) if len(nm) else code)
    c = _basemap(COUNTRIES)
    hit = c[c["ISO_A2_EH"] == code]
    if not len(hit):
        raise RegionError(f"no country {code} (ISO 3166-1 alpha-2)")
    return hit.geometry.union_all(), "Natural Earth 1:110m countries (bundled)", str(hit["NAME"].iloc[0])


FINER_COUNTIES = "ref.boundary.us.county.cb500k"


def _finer_us_counties():
    """US counties at 1:500,000 (the library's cb500k entry) when it is cached on this computer, else the bundled ones: a region keeps the
    finest boundary it can read without a fetch (Day 20's ten counties: 11,400 cells, as prep.py counted them)."""
    try:
        import ref_fetch
        e = ref_fetch.find_entry(FINER_COUNTIES)
        p = ref_fetch.cached_path(e)
        if p is not None:
            if p.name not in _CACHE:
                import geopandas as gpd
                g = gpd.read_parquet(p) if p.suffix == ".parquet" else gpd.read_file(p)
                if "STUSPS" not in g.columns and "STATEFP" in g.columns:
                    st = _basemap(US_STATES)[["STATEFP", "STUSPS"]]
                    g = g.merge(st, on="STATEFP", how="left")
                _CACHE[p.name] = g
            return _CACHE[p.name], "US Census cartographic boundary counties 1:500,000 (library cache)"
    except Exception:  # noqa: BLE001 - the bundled counties stand in
        pass
    return _basemap(US_COUNTIES), "US Census cartographic boundary counties (bundled)"


def admin2_shape(block: dict, base: Path | None = None, fetch=None):
    parent = str(block.get("in") or "").upper()
    names = [str(n) for n in block.get("names") or []]
    if not parent or not names:
        raise RegionError("region admin2 needs in: <admin-1 code> and names: [...]")
    if parent.startswith("US-"):
        cn, src_name = _finer_us_counties()
        pool = cn[cn["STUSPS"] == parent[3:]]
        key = {_norm(n).replace(" county", ""): n for n in pool["NAME"]}
        missing = [n for n in names if _norm(n).replace(" county", "") not in key]
        if missing:
            raise RegionError(f"region admin2: no county {', '.join(missing)} in {parent} (the Census names are: {', '.join(sorted(pool['NAME'])[:12])}, ...)")
        hit = pool[pool["NAME"].isin([key[_norm(n).replace(' county', '')] for n in names])]
        return hit.to_crs("EPSG:4326").geometry.union_all(), src_name, hit
    if fetch is None:
        raise RegionError(f"region admin2 outside the United States ({parent}) reads geoBoundaries ADM2: fetch it with "
                          f"ref_fetch.py fetch ref.boundary.world.admin2.geoboundaries --variant {parent.split('-')[0]}, then name the file "
                          "(region: {file: ...}) or let the build fetch it", gc.EXIT_REFUSED, "MAP-REG-002")
    gdf, src = fetch(parent)
    col = next((c for c in ("shapeName", "name", "NAME") if c in gdf.columns), None)
    hit = gdf[gdf[col].map(_norm).isin({_norm(n) for n in names})] if col else gdf.iloc[0:0]
    if not len(hit):
        raise RegionError(f"region admin2: none of {', '.join(names)} found in {parent}")
    return hit.to_crs("EPSG:4326").geometry.union_all(), src, hit


def _buffer(geom, km: float):
    import geopandas as gpd
    c = geom.centroid
    aeqd = f"+proj=aeqd +lat_0={c.y} +lon_0={c.x} +datum=WGS84 +units=m"
    g = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(aeqd).buffer(km * 1000.0).to_crs("EPSG:4326")
    return g.iloc[0]


# ---------------------------------------------------------------------------------------------------------------- resolving
def resolve(block, base: Path | None = None, *, fetch_admin2=None) -> Region:
    """One region block (or a string: a place name or an admin code) -> Region."""
    if isinstance(block, str):
        block = {"admin": block} if CODE.match(block.strip().upper()) else {"place": block}
    if not isinstance(block, dict) or not any(k in block for k in KEYS):
        raise RegionError(f"region needs one of {', '.join(KEYS)}")
    from shapely.ops import unary_union
    geoms, sources, notes, members = [], [], [], []
    name, definition = None, None
    if "place" in block:
        r = find_place(str(block["place"]), base)
        if r is None:
            near = [g["name"] for g in gazetteer(base) if _norm(block["place"])[:4] in _norm(g["name"])][:5]
            raise RegionError(f"no named region '{block['place']}' in the gazetteer" + (f" (did you mean {', '.join(near)}?)" if near else "")
                              + "; add it to .geo-mapping/regions.yaml or give admin: codes")
        name = r["name"]
        definition = r.get("note") or f"{len(r['members'])} member area(s)"
        if r.get("origin") == "yours":
            definition += " (your own regions file)"
        for m in r["members"]:
            if isinstance(m, dict):
                g, s, hit = admin2_shape(m, base, fetch_admin2)
                members += [f"{m['in']}:{n}" for n in m["names"]]
            else:
                g, s, _n = admin_shape(m, base)
                members.append(str(m))
            geoms.append(g)
            sources.append(s)
        notes.append(f"region '{name}': {definition}")
    if "admin" in block:
        codes = block["admin"] if isinstance(block["admin"], (list, tuple)) else [block["admin"]]
        nm = []
        for c in codes:
            g, s, n = admin_shape(str(c), base)
            geoms.append(g)
            sources.append(s)
            nm.append(n)
            members.append(str(c).upper())
        name = name or (" and ".join(nm) if len(nm) < 4 else ", ".join(nm[:3]) + f" and {len(nm) - 3} more")
        definition = definition or ", ".join(str(c).upper() for c in codes)
    if "admin2" in block:
        g, s, hit = admin2_shape(block["admin2"], base, fetch_admin2)
        geoms.append(g)
        sources.append(s)
        members += [f"{block['admin2']['in']}:{n}" for n in block["admin2"]["names"]]
        name = name or f"{len(block['admin2']['names'])} areas of {block['admin2']['in']}"
        definition = definition or ", ".join(block["admin2"]["names"])
    if "file" in block:
        import geo_io
        p = Path(str(block["file"]))
        p = p if p.is_absolute() or base is None else base / p
        if not p.is_file():
            raise RegionError(f"region file {block['file']} not found")
        r = geo_io.read(p)
        gdf = r.gdf.to_crs("EPSG:4326") if r.gdf.crs is not None else r.gdf.set_crs("EPSG:4326")
        geoms.append(gdf.geometry.union_all())
        sources.append(f"the person's file {p.name}")
        name = name or p.stem.replace("_", " ")
        definition = definition or f"the shapes in {p.name}"
    if "bbox" in block:
        from shapely.geometry import box
        b = [float(v) for v in block["bbox"]]
        geoms.append(box(*b))
        sources.append("a box")
        name = name or "the box " + ", ".join(f"{v:g}" for v in b)
        definition = definition or "a lon/lat box"
    if "h3" in block:
        import hexbin
        import shapely
        h3 = hexbin._h3()
        cells = [str(c) for c in block["h3"]]
        geoms.append(unary_union([shapely.Polygon([(lng, lat) for lat, lng in h3.cell_to_boundary(c)]) for c in cells]))
        sources.append("H3 cells")
        name = name or f"{len(cells)} H3 cell(s)"
        definition = definition or "H3 cells"
    geom = unary_union(geoms)
    if block.get("buffer_km"):
        geom = _buffer(geom, float(block["buffer_km"]))
        definition += f", grown by {float(block['buffer_km']):g} km"
    if not geom.is_valid:
        geom = geom.buffer(0)
    reg = Region(geometry=geom, name=str(block.get("name") or name), definition=str(definition), sources=list(dict.fromkeys(sources)),
                 notes=notes, members=members)
    _partitions(reg)
    return reg


def _partitions(reg: Region) -> None:
    """countries, admin1 and us_states the region touches (G-04: each part through its own country's adapter)."""
    a1 = _basemap(ADMIN1)
    g = reg.geometry
    inner = g.buffer(-INSET_DEG)                     # two boundary sources never line up exactly: a shared border is not a partition
    if not inner.is_empty and inner.area > g.area * 0.2:
        g = inner
    try:
        cand = a1.iloc[a1.sindex.query(g, predicate="intersects")]
    except Exception:  # noqa: BLE001
        cand = a1[a1.geometry.intersects(g)]
    keep = []
    ga = g.area or 1e-12
    for _i, row in cand.iterrows():
        inter = row.geometry.intersection(g).area
        if inter / (row.geometry.area or 1e-12) > SLIVER or inter / ga > SLIVER:
            keep.append(row)
    reg.admin1 = sorted({str(r["iso_3166_2"]) for r in keep if r["iso_3166_2"] and str(r["iso_3166_2"]) != "-99"})
    reg.countries = sorted({str(r["iso_a2"]) for r in keep if r["iso_a2"] and str(r["iso_a2"]) not in ("-99", "-1")})
    reg.partitions = {"countries": reg.countries, "admin1": reg.admin1,
                      "us_states": sorted({c[3:] for c in reg.admin1 if c.startswith("US-")})}


def region_of(spec: dict, base: Path | None = None) -> Region | None:
    blk = spec.get("region")
    if not blk:
        return None
    return resolve(blk, base)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="What a region resolves to: name, definition, countries, admin-1 areas, partitions (no map)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--spec")
    g.add_argument("--place")
    g.add_argument("--admin", help="comma-separated ISO codes")
    ap.add_argument("--list", action="store_true", help="every named region in the gazetteer")
    a = ap.parse_args(argv)
    try:
        if a.spec:
            sp = Path(a.spec)
            r = region_of(gc.load_mapspec(sp), sp.parent)
            if r is None:
                print(json.dumps({"ok": False, "message": "the MapSpec has no region: block"}))
                return gc.EXIT_USAGE
        else:
            r = resolve({"place": a.place} if a.place else {"admin": [c.strip() for c in a.admin.split(",") if c.strip()]})
    except RegionError as ex:
        print(json.dumps({"ok": False, "findings": [{"id": ex.rule, "severity": "BLOCK", "message": str(ex)}]}, indent=2))
        return ex.code
    print(json.dumps({"ok": True, **r.record(), "notes": r.notes}, indent=2))
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
