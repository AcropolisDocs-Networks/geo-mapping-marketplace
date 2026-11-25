#!/usr/bin/env python3
"""region: the place a map is about, resolved once (2.6.0 for the H3 complete grid; 3.0.0, spec global-regions G-01 to G-05).

    region: {admin: US-WA}                         an ISO 3166-1 country (KE) or ISO 3166-2 admin-1 area (US-WA, DE-BY, CA-BC)
    region: {admin: [US-WA, US-OR, CA-BC]}          several, across borders (G-04)
    region: {admin2: {in: US-ID, names: [Bonner, Boundary]}}    admin-2 areas inside one admin-1 area
    region: "Maricopa County, Arizona"   "Orleans Parish, LA"   "04013"     4.0.3: a US county by its plain name (with its state when
                                                    the name is in more than one) or its 5-digit FIPS code
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
import os
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


def drop_specks(geom, share: float = 0.005, reach: float = 0.2):
    """3.1.0: a (multi)polygon without its remote specks: parts under `share` of the area that lie farther from the rest than `reach` times the
    rest's size (Rockall off Scotland, the Prince Edward Islands of the Western Cape). Any CRS; the geometry itself when nothing is dropped."""
    parts = sorted(getattr(geom, "geoms", [geom]), key=lambda g: g.area, reverse=True)
    if len(parts) < 2:
        return geom
    total = sum(g.area for g in parts) or 1.0
    from shapely.ops import unary_union
    keep = [g for g in parts if g.area >= share * total]
    core = unary_union(keep)
    W, S, E, N = core.bounds
    far = reach * max(E - W, N - S)
    small = [g for g in parts if g.area < share * total]
    near = [g for g in small if g.distance(core) <= far]
    return geom if len(near) == len(small) else unary_union(keep + near)


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
    findings: list = field(default_factory=list)            # 3.4.0: MAP-REG-004 when a US region fell back to the coarse outline

    @property
    def bounds(self) -> tuple:
        return tuple(float(v) for v in self.geometry.bounds)

    @property
    def frame_geometry(self):
        """3.1.0: the region without its remote specks (Rockall off Scotland), so the frame fits the land people mean. The data and the
        outline keep every part."""
        return drop_specks(self.geometry)

    @property
    def frame_bounds(self) -> tuple:
        return tuple(float(v) for v in self.frame_geometry.bounds)

    @property
    def id(self) -> str:
        """3.1.0: a file-name-safe slug of the region's name (adapters name their cached files by it)."""
        import re as _re
        return _re.sub(r"[^a-z0-9]+", "-", str(self.name).lower()).strip("-")[:60] or "region"

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


_FALLBACK: list = []          # 3.4.0 (Day 20 #16): why the finer counties were not used, read once per resolve()


def _finer_us_counties():
    """US counties at 1:500,000 (the library's cb500k entry), fetched into the library cache on first use (one file, about 3 MB; 3.4.0, Day
    20 #16: 3.0.0 used them only when cached, so cell counts changed silently from 38,409 to 37,652), else the bundled ones with a reason in
    _FALLBACK that resolve() turns into MAP-REG-004."""
    try:
        import ref_fetch
        e = ref_fetch.find_entry(FINER_COUNTIES)
        p = ref_fetch.cached_path(e)
        if p is None and os.environ.get("GMAP_OFFLINE") != "1":
            try:
                p = Path(ref_fetch.fetch(e)["path"])
            except Exception as ex:  # noqa: BLE001 - no network: the bundled counties stand in, said loudly
                _FALLBACK.append(("WARN", f"the 1:500,000 counties could not be fetched ({type(ex).__name__}: {str(ex)[:120]})"))
                p = None
        elif p is None:
            _FALLBACK.append(("INFO", "offline (GMAP_OFFLINE=1): the 1:500,000 counties are not cached"))
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


COUNTY_WORDS = ("city and borough", "census area", "municipality", "borough", "parish", "county", "city")


def _cnorm(s: str) -> str:
    """4.0.3: a county name compared loosely: case, accents and punctuation aside, and Saint as St (St. Louis County, Saint Louis County)."""
    return re.sub(r"\bsaint\b", "st", _norm(s))


def _bare(s: str) -> str:
    """A county name without its county word (Maricopa County -> maricopa, Orleans Parish -> orleans)."""
    n = _cnorm(s)
    for w in COUNTY_WORDS:
        if n.endswith(" " + w):
            return n[: -len(w) - 1].strip()
    return n


def _county_rows(pool, name: str):
    """4.0.3: the rows of one county name: by its full Census name first (NAMELSAD: Baltimore County, not also Baltimore city), else by the
    name without its county word."""
    if "NAMELSAD" in pool.columns:
        full = pool[pool["NAMELSAD"].map(_cnorm) == _cnorm(name)]
        if len(full):
            return full
    return pool[pool["NAME"].map(_bare) == _bare(name)]


def admin2_shape(block: dict, base: Path | None = None, fetch=None):
    parent = str(block.get("in") or "").upper()
    names = [str(n) for n in block.get("names") or []]
    if not parent or not names:
        raise RegionError("region admin2 needs in: <admin-1 code> and names: [...]")
    if parent.startswith("US-"):
        cn, src_name = _finer_us_counties()
        pool = cn[cn["STUSPS"] == parent[3:]]
        rows = {n: _county_rows(pool, n) for n in names}
        missing = [n for n, r in rows.items() if not len(r)]
        if missing:
            raise RegionError(f"region admin2: no county {', '.join(missing)} in {parent} (the Census names are: {', '.join(sorted(pool['NAME'])[:12])}, ...)")
        import pandas as pd
        hit = pd.concat(list(rows.values()))
        hit = hit[~hit.index.duplicated()]
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


GEOBOUNDARIES = "ref.boundary.world.country.geoboundaries"


def iso3_of(iso2: str) -> str | None:
    """3.1.0: ISO alpha-3 (lower case) of an alpha-2 code, from data/countries.yaml; None when unknown."""
    try:
        cs = gc.load_yaml(gc.plugin_root() / "data" / "countries.yaml")["countries"]
        return str(cs[str(iso2).upper()]["iso3"]).lower()
    except Exception:  # noqa: BLE001
        return None


def geoboundaries_admin2(parent: str):
    """3.1.0 (G-05): admin-2 areas of any country from geoBoundaries (gbOpen, CC BY 4.0 or more open), fetched through the library and cached;
    for an admin-1 parent (DE-BY) only the areas inside it. -> (GeoDataFrame, source text). RegionError MAP-REG-002 when it cannot be read."""
    import geopandas as gpd
    import ref_fetch
    iso2 = str(parent).upper().split("-")[0]
    try:
        cs = gc.load_yaml(gc.plugin_root() / "data" / "countries.yaml")["countries"]
        iso3 = str(cs[iso2]["iso3"]).lower()
    except Exception:  # noqa: BLE001
        raise RegionError(f"no ISO alpha-3 code is known for {iso2}", gc.EXIT_USAGE, "MAP-REG-002") from None
    try:
        r = ref_fetch.fetch_entry(GEOBOUNDARIES, variant=iso3, level="adm2")
    except Exception as ex:  # noqa: BLE001 - offline, or the country's licence is not open enough
        raise RegionError(f"admin-2 areas of {iso2} come from geoBoundaries ADM2, which could not be read here ({str(ex).splitlines()[0][:160]}); fetch it on a "
                          f"computer with network: ref_fetch.py fetch {GEOBOUNDARIES} --variant {iso3} --level adm2", gc.EXIT_REFUSED, "MAP-REG-002") from None
    path = Path(r["path"])
    g = gpd.read_parquet(path) if path.suffix == ".parquet" else gpd.read_file(path)
    g = g.to_crs("EPSG:4326")
    if "-" in str(parent):
        a1 = _basemap(ADMIN1)
        sh = a1[a1["iso_3166_2"] == str(parent).upper()]
        if len(sh):
            pts = g.geometry.representative_point()
            g = g[pts.within(sh.geometry.union_all().buffer(0.01))]
    return g, f"geoBoundaries ADM2 ({iso2}, gbOpen)"


def units_in(region: "Region", level: str, *, resolution: int = 5, base: Path | None = None):
    """3.1.0 (G-05): the areas of one level inside a region, for a series (map-atlas by:, market-map): a GeoDataFrame with code, name and
    geometry (EPSG:4326). level admin1 (ISO 3166-2 areas), admin2 (US counties; elsewhere geoBoundaries ADM2) or h3 (cells at `resolution`)."""
    import geopandas as gpd
    lvl = str(level).lower()
    geom = region.geometry
    if lvl in ("admin1", "state", "province"):
        a1 = _basemap(ADMIN1)
        sel = a1[a1["iso_3166_2"].isin(region.admin1)] if region.admin1 else a1[a1["iso_a2"].isin(region.countries)]
        sel = sel[sel.geometry.intersects(geom)]
        return gpd.GeoDataFrame({"code": sel["iso_3166_2"].to_numpy(), "name": sel["name"].to_numpy()}, geometry=sel.geometry.to_numpy(), crs="EPSG:4326")
    if lvl in ("admin2", "county", "district"):
        parents = list(region.admin1) or list(units_in(region, "admin1")["code"])
        frames = []
        for par in parents:
            if par.startswith("US-"):
                cn, _src = _finer_us_counties()
                pool = cn[cn["STUSPS"] == par[3:]].to_crs("EPSG:4326")
                code = pool["GEOID"] if "GEOID" in pool.columns else pool["NAME"]
                frames.append(gpd.GeoDataFrame({"code": code.to_numpy(), "name": pool["NAME"].to_numpy()}, geometry=pool.geometry.to_numpy(), crs="EPSG:4326"))
            else:
                g, _src = geoboundaries_admin2(par)
                cc = next((c for c in ("shapeISO", "shapeID") if c in g.columns), None)
                nc = next((c for c in ("shapeName", "name", "NAME") if c in g.columns), None)
                frames.append(gpd.GeoDataFrame({"code": (g[cc] if cc else g.index.astype(str)).astype(str).to_numpy(), "name": (g[nc] if nc else g.index.astype(str)).to_numpy()},
                                               geometry=g.geometry.to_numpy(), crs="EPSG:4326"))
        import pandas as pd
        out = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs="EPSG:4326") if frames else gpd.GeoDataFrame({"code": [], "name": []}, geometry=[], crs="EPSG:4326")
        return out[out.geometry.intersects(geom)].reset_index(drop=True)
    if lvl == "h3":
        import h3_source as hs
        cells, _sh = hs.complete_grid(geom, int(resolution), "centre")
        g = hs.cells_gdf(__import__("pandas").DataFrame(index=__import__("pandas").Index(cells, name="h3")), int(resolution))
        return gpd.GeoDataFrame({"code": list(cells), "name": list(cells)}, geometry=g.geometry.to_numpy(), crs="EPSG:4326")
    raise RegionError(f"units: level is admin1, admin2 or h3, not '{level}'")


def _buffer(geom, km: float):
    import geopandas as gpd
    c = geom.centroid
    aeqd = f"+proj=aeqd +lat_0={c.y} +lon_0={c.x} +datum=WGS84 +units=m"
    g = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(aeqd).buffer(km * 1000.0).to_crs("EPSG:4326")
    return g.iloc[0]


# ---------------------------------------------------------------------------------------------------------------- 4.0.0: a town by name
TOWN_BUFFER_KM = 10.0
US_POSTAL = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD "
                "TN TX UT VT VA WA WV WI WY PR".split())
TOWN_IDS = ("ref.gazetteer.world.place.geonames-cities1000", "ref.gazetteer.world.place.geonames")


def town_place(text: str, *, fetch: bool = True) -> dict | None:
    """{name, lon, lat, where, source} for one town or city by name from GeoNames (cities1000, else cities15000), or None. "Bellevue" alone
    matches several: the name can carry its country or state ("Bellevue, US-WA", "Bellevue, WA", "Bellevue, US"); still more than one is
    None, and the error asks for the qualifier. Read from the library cache (fetched once when it is not there and the network allows)."""
    parts = [p.strip() for p in str(text).split(",") if p.strip()]
    if not parts:
        return None
    nm, qual = parts[0], (parts[1].upper() if len(parts) > 1 else "")
    cc, a1 = "", ""
    if "-" in qual:
        cc, a1 = qual.split("-", 1)
    elif qual in US_POSTAL:                      # GeoNames' admin1 code for a US state is its postal code
        cc, a1 = "US", qual
    elif len(qual) == 2:
        cc = qual
    elif qual:                                   # 5.0.0: the state or country in words ("Tempe, Arizona", "Lyon, France")
        try:
            sc = us_state_code(parts[1])
            code = ("US-" + sc) if sc else (admin_by_name(parts[1]) or "")
        except Exception:  # noqa: BLE001 - a name two areas share: the town's own name decides, or nothing does
            code = ""
        if code.startswith("US-"):
            cc, a1 = "US", code[3:]
        elif code:
            cc = code.split("-")[0]
    for eid in TOWN_IDS:
        try:
            import map_plan as mp
            import ref_fetch
            e = ref_fetch.find_entry(eid)
            if ref_fetch.cached_path(e) is None:
                if not fetch:
                    continue
                ref_fetch.fetch_entry(eid)
            g, _e = mp.load_ref_layer(eid)
        except Exception:  # noqa: BLE001 - no GeoNames here: the gazetteer's answer stands
            continue
        names = g["name"].astype(str).str.lower()
        ascii_ = g["asciiname"].astype(str).str.lower() if "asciiname" in g.columns else names
        hit = g[(names == nm.lower()) | (ascii_ == nm.lower())]
        if cc and "country_code" in hit.columns:
            hit = hit[hit["country_code"].astype(str).str.upper() == cc]
        if a1 and "admin1_code" in hit.columns:
            hit = hit[hit["admin1_code"].astype(str).str.upper() == a1]
        if len(hit) != 1:
            return None
        row = hit.iloc[0]
        pt = row.geometry
        where = "-".join(x for x in (str(row.get("country_code") or ""), str(row.get("admin1_code") or "")) if x)
        return {"name": str(row["name"]), "lon": float(pt.x), "lat": float(pt.y), "where": where, "source": "GeoNames (CC BY 4.0)"}
    return None


# ---------------------------------------------------------------------------------------------------------------- 4.0.3: a US county by name
FIPS = re.compile(r"^\d{5}$")
COUNTRY_WORDS = {"us", "usa", "u s", "u s a", "united states", "united states of america"}


def us_state_code(text: str) -> str | None:
    """'AZ', 'US-AZ' or 'Arizona' -> 'AZ'; None when it is not a US state (or DC, PR)."""
    t = str(text or "").strip()
    u = t.upper()
    if u.startswith("US-"):
        u = u[3:]
    if u in US_POSTAL:
        return u
    st = _basemap(US_STATES)
    hit = st[st["NAME"].map(_norm) == _norm(t)]
    return str(hit["STUSPS"].iloc[0]) if len(hit) else None


def county_place(text: str, *, need_word: bool = True) -> dict | None:
    """4.0.3: a US county named in plain words -> {in: US-XX, names: [Census name], label: "Maricopa County, Arizona"}, else None.

    "Maricopa County", "Maricopa County, Arizona", "Maricopa County, AZ, USA", "Orleans Parish, LA", "Juneau City and Borough", "04013" (FIPS).
    With need_word (the default) the name must end in a county word (County, Parish, Borough, Census Area, Municipality, or an
    independent city as the Census names it: "Baltimore city"); without it ("Maricopa, AZ") it is the fallback after a town of that name was
    not found. A name in several states without its state raises RegionError naming them."""
    t = str(text or "").strip()
    cn, _src = _finer_us_counties()
    if FIPS.match(t):
        hit = cn[cn["GEOID"].astype(str) == t]
        if not len(hit):
            return None
        row = hit.iloc[0]
    else:
        parts = [p.strip() for p in t.split(",") if p.strip()]
        while len(parts) > 1 and _norm(parts[-1]) in COUNTRY_WORDS:
            parts.pop()
        if not parts or len(parts) > 2:
            return None
        name = parts[0]
        if need_word and not any(_cnorm(name).endswith(" " + w) for w in COUNTY_WORDS):
            return None
        pool = cn
        if len(parts) == 2:
            code = us_state_code(parts[1])
            if code is None:
                return None
            pool = cn[cn["STUSPS"] == code]
        hit = _county_rows(pool, name) if need_word else pool[pool["NAME"].map(_bare) == _bare(name)]
        if need_word and _cnorm(name).endswith(" city") and "NAMELSAD" in pool.columns:
            hit = pool[pool["NAMELSAD"].map(_cnorm) == _cnorm(name)]       # "Carson City" is a town unless the Census names it so
        if not len(hit):
            return None
        if hit["STUSPS"].nunique() > 1:
            states = sorted(hit["STUSPS"].astype(str).unique())
            raise RegionError(f"'{t}' is a county in {len(states)} states ({', '.join(states[:12])}{', ...' if len(states) > 12 else ''}): "
                              f"name the state, as in \"{parts[0]}, {states[0]}\"")
        if len(hit) > 1:
            return None                                  # Baltimore (county and city) without the county word: not one county
        row = hit.iloc[0]
    st = _basemap(US_STATES)
    sn = st[st["STUSPS"] == str(row["STUSPS"])]["NAME"]
    full = str(row.get("NAMELSAD") or row["NAME"])
    return {"in": f"US-{row['STUSPS']}", "names": [full], "label": f"{full}, {sn.iloc[0] if len(sn) else row['STUSPS']}",
            "geoid": str(row.get("GEOID") or "")}


def admin_by_name(text: str) -> str | None:
    """4.0.3: a country, US state or other admin-1 area named in plain words -> its ISO code ("Arizona" -> US-AZ, "Kenya" -> KE, "Bavaria" ->
    DE-BY, "British Columbia" -> CA-BC), else None. 4.0.0 sent such names to the town lookup: "Arizona" became a town in Honduras. A name
    shared by areas in different places ("Georgia": the country and the US state) raises RegionError naming each code; a country after a comma
    picks one ("Georgia, US", "Punjab, PK")."""
    parts = [p.strip() for p in str(text or "").split(",") if p.strip()]
    if not parts or len(parts) > 2:
        return None
    want = _norm(parts[0])
    if want in COUNTRY_WORDS and len(parts) == 1:
        return "US"
    only = None
    if len(parts) == 2:
        q = parts[1]
        only = "US" if _norm(q) in COUNTRY_WORDS else (q.upper() if re.fullmatch(r"[A-Za-z]{2}", q) else None)
        if only is None:
            return None
    codes: dict[str, str] = {}
    st = _basemap(US_STATES)
    for _i, row in st[st["NAME"].map(_norm) == want].iterrows():
        codes[f"US-{row['STUSPS']}"] = f"{row['NAME']} (US state)"
    a1 = _basemap(ADMIN1)
    m = (a1["name"].map(_norm) == want) | (a1["name_en"].map(_norm) == want)
    for _i, row in a1[m].iterrows():
        c = str(row["iso_3166_2"] or "").upper()
        if c and not c.startswith("US-") and CODE.match(c):
            codes.setdefault(c, f"{row['name']} ({row['admin']})")
    cs = _basemap(COUNTRIES)
    for _i, row in cs[cs["NAME"].map(_norm) == want].iterrows():
        c = str(row["ISO_A2_EH"] or "").upper()
        if re.fullmatch(r"[A-Z]{2}", c):
            codes.setdefault(c, f"{row['NAME']} (country)")
    if only:
        codes = {c: d for c, d in codes.items() if c.split("-")[0] == only}
    if not codes:
        return None
    if len(codes) > 1:
        opts = "; ".join(f"{d}: {c}" for c, d in sorted(codes.items()))
        raise RegionError(f"'{text}' names more than one area ({opts}): give the code, as in admin: {sorted(codes)[0]}, or add the "
                          f"country (\"{parts[0]}, {sorted(codes)[0].split('-')[0]}\")")
    return next(iter(codes))


# ---------------------------------------------------------------------------------------------------------------- resolving
def resolve(block, base: Path | None = None, *, fetch_admin2=None) -> Region:
    """One region block (or a string: a place name or an admin code) -> Region."""
    if isinstance(block, str):
        block = {"admin": block} if CODE.match(block.strip().upper()) else {"place": block}
    if isinstance(block, dict) and "admin" in block and isinstance(block["admin"], str) and FIPS.match(block["admin"].strip()):
        block = {**{k: v for k, v in block.items() if k != "admin"}, "place": block["admin"].strip()}     # 4.0.3: a county FIPS code
    if not isinstance(block, dict) or not any(k in block for k in KEYS):
        raise RegionError(f"region needs one of {', '.join(KEYS)}")
    from shapely.ops import unary_union
    _FALLBACK.clear()
    geoms, sources, notes, members = [], [], [], []
    name, definition = None, None
    county = None
    if "place" in block and find_place(str(block["place"]), base) is None:
        code = admin_by_name(str(block["place"]))           # 4.0.3: "Arizona", "Kenya", "Bavaria": the area, not a town of that name
        if code is not None:
            block = {**{k: v for k, v in block.items() if k != "place"}, "admin": code}
    if "place" in block:
        r = find_place(str(block["place"]), base)
        town = None
        if r is None:
            county = county_place(str(block["place"]))     # 4.0.3: "Maricopa County, Arizona", "Orleans Parish", "04013"
        if r is None and county is None:
            town = town_place(str(block["place"]))         # 4.0.0 (GSC M-3): a town or city by name (GeoNames), framed with buffer_km
        if r is None and county is None and town is None and "," in str(block["place"]):
            county = county_place(str(block["place"]), need_word=False)   # "Yavapai, AZ" when no town has that name (with its state only)
        if county is not None:
            name, definition = county["label"], f"{county['names'][0]} ({county['in']}, US Census counties)"
            block = {**{k: v for k, v in block.items() if k != "place"}, "admin2": {"in": county["in"], "names": county["names"]}}
        elif r is None and town is None:
            near = [g["name"] for g in gazetteer(base) if _norm(block["place"])[:4] in _norm(g["name"])][:5]
            raise RegionError(f"no named region '{block['place']}' in the gazetteer, and no single town of that name in GeoNames"
                              + (f" (did you mean {', '.join(near)}?)" if near else "")
                              + "; add it to .geo-mapping/regions.yaml, give admin: codes, name the town with its state or country "
                                "(\"Bellevue, US-WA\"), or name a US county with its state (\"Maricopa County, AZ\")")
    if "place" in block and town is not None:
        from shapely.geometry import Point
        geoms.append(Point(town["lon"], town["lat"]))
        sources.append(town["source"])
        name = town["name"]
        definition = f"the town of {town['name']} ({town['where']}, GeoNames)"
        members.append(town["name"])
        block = {**block, "buffer_km": block.get("buffer_km") or TOWN_BUFFER_KM}
        notes.append(f"region '{name}': {definition}, framed {float(block['buffer_km']):g} km around it")
    elif "place" in block:
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
        if county is not None:
            notes.append(f"region '{name}': the county {county['names'][0]} ({county['in']}, FIPS {county['geoid']})")
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
    for sev, why in dict.fromkeys(_FALLBACK):                 # 3.4.0 (Day 20 #16): never a silent coarse outline
        reg.findings.append({"id": "MAP-REG-004", "severity": "WARN" if sev == "WARN" else "INFO", "message": f"region {reg.name}: {why}, so its US outline comes from the bundled "
                             "coarser counties and an H3 grid completed to it counts cells differently (Washington: 38,409 instead of 37,652); "
                             f"run ref_fetch.py fetch {FINER_COUNTIES} on a computer with network, then build again"})
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
