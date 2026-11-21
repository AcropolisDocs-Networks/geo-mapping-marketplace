"""Mobile coverage and network adapters outside the United States (3.1.0, G-12): each with its terms read and its download route checked.

    bnetza-mobile-coverage    Germany. Bundesnetzagentur Mobilfunk-Monitoring: 100 m grid cells (BKG grid, ETRS89 / UTM 32N) with the number
                              of operators offering 2G, 4G, 5G and 5G SA. Datenlizenz Deutschland - Namensnennung 2.0 (commercial use with
                              the credit "© Bundesnetzagentur"). One zip (about 100 MB) a quarter, found on the Gigabit-Grundbuch download page.
    arcep-mon-reseau-mobile   France. Arcep "Mon reseau mobile" theoretical coverage: one GeoPackage per operator, technology and use (7z),
                              polygons by level (TBC very good, BC good, CL limited), Lambert-93 in metropolitan France. Licence Ouverte 2.0.
                              Listed from the public bucket behind data.arcep.fr; metropolitan 4G files are 130 to 390 MB each.
    accc-mobile-coverage      Australia. ACCC Mobile Infrastructure Report data release on data.gov.au: coverage polygons per operator,
                              technology and environment (outdoor, external antenna), one KML or zip a year. CC BY 2.5 Australia.
    canada-wireless-coverage  Canada. LTE and 5G availability (CRTC data, 2024) from the Atlas of Canada's Wireless Data Network service
                              (Natural Resources Canada). Open Government Licence - Canada. Not by provider: the map shows where LTE and
                              5G are available, never a carrier choice.
    ofcom-connected-nations   United Kingdom. Ofcom Connected Nations, by local authority: the share of land (or premises) covered by 0 to 4
                              operators, from Ofcom's laua CSV (Open Government Licence), on ONS local authority boundaries (OGL). Ofcom's
                              site refuses scripted downloads, so the CSV is downloaded in a browser and put in data/ofcom-connected-nations/.
                              Ofcom does not publish its 50 m pixel predictions (FOI, August 2026), so there is no UK H3 grid: the map is
                              by council. Postcode checks for a site list go through Ofcom's API with the person's key (ofcom_postcodes).
    opencellid                World. OpenCelliD cell towers per mobile country code, with the person's opencellid token (CC BY-SA 4.0,
                              share-alike); cells on H3 by network (MCC-MNC, named from data/mcc-mnc.csv).
    ookla-open-data           World. Ookla Speedtest open data, mobile or fixed tiles (zoom 16) per quarter, on H3 by tile centre:
                              mean download, upload or latency weighted by tests. CC BY-NC 4.0: internal maps only.

The build reads what is already in the work folder (data/<adapter>/); the fetch is a separate step with a cost card first:
`adapters_cli.py fetch NAME --region ...` or the map server's coverage_fetch tool.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gmap_common as gc  # noqa: E402
from adapters import Adapter, AdapterError  # noqa: E402

MAX_DOWNLOAD = 2_500_000_000          # one file; the cost card is shown before anything this large is fetched
UA = "geo-mapping-plugin (coverage adapters)"


# ---------------------------------------------------------------------------------------------------------------- shared helpers
def region_now(required: bool = True):
    import map_plan as mp
    reg = mp._BUILD.get("region")
    if reg is None and required:
        raise AdapterError("this adapter needs a region: block (the map's place); the data is read for that region only", 2)
    return reg


def folder_for(src: dict, base: Path, kind: str) -> Path:
    f = Path(src.get("path") or f"data/{kind}")
    return f if f.is_absolute() else Path(base) / f


def download(url: str, dest: Path, *, max_bytes: int = MAX_DOWNLOAD, auth=None, opener=None) -> dict:
    """Stream an HTTPS file to disk (a .part file renamed when complete). A credential, when given, is added at send time and never recorded."""
    u = urllib.parse.urlparse(url)
    if u.scheme != "https":
        raise AdapterError("only HTTPS downloads are allowed", 5)
    send, headers = url, {"User-Agent": UA}
    if auth is not None:
        if (u.hostname or "").lower() != auth.host:              # a credential goes only to its registered host (MAP-CRED-013)
            raise AdapterError(f"the credential '{auth.name}' is registered for {auth.host}, not {u.hostname}", 3, "MAP-CRED-013")
        send, headers = auth.apply(url, headers)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h, total = hashlib.sha256(), 0
    try:
        with (opener or urllib.request.urlopen)(urllib.request.Request(send, headers=headers), timeout=300) as r, open(part, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise AdapterError(f"{dest.name} is larger than {max_bytes // 1_000_000:,} MB; stopped", 5)
                h.update(chunk)
                f.write(chunk)
    except AdapterError:
        part.unlink(missing_ok=True)
        raise
    except Exception as ex:  # noqa: BLE001 - network, TLS and HTTP errors, said without the address's query (a key may be in it)
        part.unlink(missing_ok=True)
        raise AdapterError(f"could not download {dest.name} from {u.netloc}: {str(ex)[:200]}", 5) from None
    part.replace(dest)
    return {"path": str(dest), "bytes": total, "sha256": h.hexdigest(), "url": urllib.parse.urlunparse(u._replace(query=""))}


def get_text(url: str, *, http=None, max_bytes: int = 20_000_000) -> str:
    import ogc_http as oh
    http = http or oh.SafeHTTP()
    return http.get(url, expect="any", max_bytes=max_bytes).text


def ids_int(cells) -> np.ndarray:
    import h3_source as hs
    return hs.ids_to_int(np.asarray(list(cells), dtype=object))


def points_to_cells(lon, lat, res: int) -> np.ndarray:
    import h3
    return np.fromiter((int(h3.latlng_to_cell(float(b), float(a), res), 16) for a, b in zip(lon, lat, strict=True)), dtype=np.uint64, count=len(lon))


def polys_to_cells(geoms, res: int, clip=None) -> list[np.ndarray]:
    """One array of H3 cell ids (centres inside) per polygon, clipped to `clip` (lon/lat) first."""
    import h3
    from shapely.geometry import mapping
    out = []
    for g in geoms:
        if g is None or g.is_empty:
            out.append(np.array([], dtype=np.uint64))
            continue
        if clip is not None:
            g = g.intersection(clip)
        parts = [p for p in getattr(g, "geoms", [g]) if p.geom_type == "Polygon" and not p.is_empty]
        cells: set = set()
        for p in parts:
            try:
                cells |= set(h3.geo_to_cells(mapping(p), res))
            except Exception:  # noqa: BLE001 - a sliver with no cell centre inside, or a ring h3 cannot read
                continue
        out.append(np.fromiter((int(c, 16) for c in cells), dtype=np.uint64, count=len(cells)))
    return out


def frame_of(cells: list[np.ndarray], groups: list[str], col: str = "provider"):
    import pandas as pd
    if not cells:
        return pd.DataFrame({"_h3": np.array([], dtype=np.uint64), col: []})
    ids = np.concatenate(cells) if cells else np.array([], dtype=np.uint64)
    gs = np.concatenate([np.full(len(c), g, dtype=object) for c, g in zip(cells, groups, strict=True)])
    return pd.DataFrame({"_h3": ids, col: gs}).drop_duplicates()


def bounds_in(region, crs: str) -> tuple:
    from pyproj import Transformer
    W, S, E, N = region.bounds
    t = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    xs, ys = zip(*[t.transform(W + (E - W) * i / 6, S + (N - S) * j / 6) for i in range(7) for j in range(7)])
    return min(xs), min(ys), max(xs), max(ys)


class FileAdapter(Adapter):
    """An adapter whose data are files fetched into the work folder (data/<kind>/) and read for the region at build time."""
    kind = "coverage"
    defaults: dict = {}
    res = 9

    def listing(self, region, **opts) -> list[dict]:
        raise AdapterError(f"{self.name}: no listing", 4)

    def cost(self, parts: dict, files: list | None = None, **kw) -> dict:
        fl = files or []
        return {"files": len(fl), "bytes": int(sum(int(f.get("bytes") or 0) for f in fl)), "names": [f["name"] for f in fl][:20]}

    def wanted(self, folder: Path, region, **opts) -> list[dict]:
        return self.listing(region, **opts)

    def fetch(self, region, folder: Path, *, confirm: bool = False, opener=None, **opts) -> dict:
        files = self.listing(region, **opts)
        card = {"adapter": self.name, "region": region.name, "cost": self.cost({}, files), "folder": str(folder), "licence": self.licence_class}
        if not confirm:
            return {"ok": True, "confirm_needed": True, "card": card}
        got = []
        for f in files:
            dest = folder / f["name"]
            if dest.is_file() and (not f.get("bytes") or dest.stat().st_size == int(f["bytes"])):
                got.append({"path": str(dest), "bytes": dest.stat().st_size, "kept": True})
                continue
            got.append(download(f["url"], dest, auth=self.auth() if self.credential else None, opener=opener))
        (folder / f"{self.name}.fetch.json").write_text(json.dumps({"adapter": self.name, "region": region.name, "files": got,
                                                                     "fetched": __import__("datetime").date.today().isoformat()}, indent=2), encoding="utf-8")
        return {"ok": True, "card": card, "files": got}

    def auth(self):
        import credentials as cr
        try:
            return cr.resolve(f"cred:{self.credential}")
        except cr.CredentialError as ex:
            raise AdapterError(ex.findings[0]["message"], 3, ex.findings[0]["id"]) from None

    def local(self, folder: Path, region, **opts) -> list[Path]:
        raise NotImplementedError

    def table(self, paths: list[Path], region, **opts):
        raise NotImplementedError

    def missing_message(self, folder: Path) -> str:
        return (f"the {self.name} files for this region are not in {folder.name}/ yet. Fetch them first (a cost card comes before the download): "
                f"the map server's coverage_fetch tool with adapter={self.name} and this region, or "
                f"adapters_cli.py fetch {self.name} --region \"<region>\" --out {folder}")

    def fetch_h3(self, src: dict, base: Path, region_geom=None) -> dict:
        reg = region_now()
        if self.coverage_countries and self.covers(reg) <= 0.0:
            raise AdapterError(f"the region {reg.name} is outside {self.name}'s coverage ({', '.join(self.coverage_countries)})", 2)
        folder = folder_for(src, base, self.name)
        paths = self.local(folder, reg, **src) if folder.is_dir() else []
        if not paths:
            raise AdapterError(self.missing_message(folder), 4)
        df, steps = self.table(paths, reg, **src)
        import map_plan as mp
        mp._BUILD.setdefault("adapter_parts", {})[self.name] = {"files": [p.name for p in paths]}
        return {"table": df, "steps": steps, "defaults": dict(self.defaults)}


# ---------------------------------------------------------------------------------------------------------------- Germany
BNETZA_PAGE = "https://gigabitgrundbuch.bund.de/GIGA/DE/Downloads_Suche/start.html"
BNETZA_LATEST = "https://data.bundesnetzagentur.de/Bundesnetzagentur/GIGA/DE/MobilfunkMonitoring/2606/202607_MobilfunkMonitoring.zip"
BNETZA_ZIP = re.compile(r"https://data\.bundesnetzagentur\.de/Bundesnetzagentur/GIGA/DE/MobilfunkMonitoring/(\d{4})/(\d{6})_MobilfunkMonitoring\.zip")
BNETZA_TECH = {"2G": "gsm_out", "4G": "lte", "4GLTE": "lte", "LTE": "lte", "5G": "nr_gesamt", "5GNR": "nr_gesamt", "5G-SA": "nr_sa", "5GSA": "nr_sa"}
BNETZA_CELL = re.compile(r"N(\d+)E(\d+)")


class BNetzA(FileAdapter):
    name = "bnetza-mobile-coverage"
    entry_id = "ref.statistic.de.coverage.bnetza-mobile"
    coverage_countries = ("DE",)
    grid = "squares"
    licence_class = "attribution"
    kind = name
    res = 10
    defaults = {"agg": "max", "value_field": "operators"}

    def listing(self, region, *, http=None, **opts) -> list[dict]:
        url = BNETZA_LATEST
        try:
            found = BNETZA_ZIP.findall(get_text(BNETZA_PAGE, http=http))
            if found:
                q, f = max(found, key=lambda x: x[1])
                url = f"https://data.bundesnetzagentur.de/Bundesnetzagentur/GIGA/DE/MobilfunkMonitoring/{q}/{f}_MobilfunkMonitoring.zip"
        except Exception:  # noqa: BLE001 - the page did not answer: the last known file
            pass
        return [{"name": url.rsplit("/", 1)[-1], "url": url, "bytes": 106_439_749 if url == BNETZA_LATEST else None}]

    def local(self, folder: Path, region, **opts) -> list[Path]:
        zs = sorted(folder.glob("*_MobilfunkMonitoring*.zip")) + sorted(folder.glob("*_MobilfunkMonitoring*.csv"))
        return zs[-1:] if zs else []

    def table(self, paths, region, technology: str = "5G", **opts):
        import pandas as pd
        import zipfile
        from pyproj import Transformer
        from shapely import contains_xy
        col = BNETZA_TECH.get(str(technology).upper().replace(" ", ""), None)
        if col is None:
            raise AdapterError(f"technology '{technology}' is not one of 2G, 4G, 5G, 5G-SA", 2)
        x0, y0, x1, y1 = bounds_in(region, "EPSG:25832")
        p = paths[0]
        if p.suffix == ".zip":
            z = zipfile.ZipFile(p)
            member = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            fh = z.open(member)
        else:
            fh = open(p, "rb")
        keep = []
        with fh:
            for chunk in pd.read_csv(fh, usecols=["id", col], chunksize=2_000_000, dtype={"id": str}):
                nm = chunk["id"].str.extract(BNETZA_CELL)
                n_, e_ = pd.to_numeric(nm[0]), pd.to_numeric(nm[1])
                x, y = e_ * 100 + 50, n_ * 100 + 50
                m = (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)
                if m.any():
                    keep.append(pd.DataFrame({"x": x[m].to_numpy(), "y": y[m].to_numpy(), "operators": chunk.loc[m, col].to_numpy()}))
        if not keep:
            raise AdapterError(f"{p.name} has no grid cell inside the region {region.name}", 4)
        d = pd.concat(keep, ignore_index=True)
        lon, lat = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True).transform(d["x"].to_numpy(), d["y"].to_numpy())
        inside = contains_xy(region.geometry, lon, lat)
        d, lon, lat = d[inside], lon[inside], lat[inside]
        df = pd.DataFrame({"_h3": points_to_cells(lon, lat, self.res), "operators": d["operators"].astype(int).to_numpy()})
        steps = [f"Bundesnetzagentur Mobilfunk-Monitoring ({p.name}): {len(df):,} 100 m grid cells in {region.name}, the number of operators offering "
                 f"{technology} ({col}) in each, placed on H3 resolution {self.res} by cell centre"]
        return df, steps


# ---------------------------------------------------------------------------------------------------------------- France
ARCEP_BUCKET = "https://arcep.s3.rbx.io.cloud.ovh.net/"
ARCEP_PREFIX = "mobile/couvertures_theoriques/"
ARCEP_OPS = {"OF": "Orange", "SFR0": "SFR", "SFR": "SFR", "BOUY": "Bouygues Telecom", "BYT": "Bouygues Telecom", "FREE": "Free Mobile",
             "DIGIC": "Digicel", "FRCA": "Free Caraïbe", "OMT": "Orange Caraïbe", "ORCA": "Orange Caraïbe", "SRR": "SFR Réunion", "TELC": "Telco OI",
             "MAOR": "Maore Mobile", "UTS": "UTS Caraïbe", "DAUPH": "Dauphin Telecom"}
ARCEP_DOM = {"GP": "971", "MQ": "972", "GF": "973", "RE": "974", "YT": "976", "BL": "977", "MF": "978"}
ARCEP_FILE = re.compile(r"(?P<q>\d{4}_T\d)_couv_(?P<terr>Metropole|\d{3})_(?P<op>[A-Z0-9]+)_(?P<tech>2G3G|2G|3G|4G|5G)_(?P<use>voix|data)\.(?:gpkg\.)?7z$")
ARCEP_LEVELS = {"very-good": ("TBC",), "good": ("TBC", "BC"), "any": ("TBC", "BC", "CL")}


class Arcep(FileAdapter):
    name = "arcep-mon-reseau-mobile"
    entry_id = "ref.statistic.fr.coverage.arcep-mobile"
    coverage_countries = ("FR", "GP", "MQ", "GF", "RE", "YT", "BL", "MF")
    grid = "polygons"
    licence_class = "open"
    kind = name
    defaults = {"by": "provider", "agg": "count_groups"}

    def territories(self, region) -> list[str]:
        """Metropole when a metropolitan department is in the region (or France as a whole), and the INSEE code of each overseas one."""
        a1 = [str(c).upper() for c in region.admin1]
        dom = {f"FR-{k}": v for k, v in ARCEP_DOM.items()}
        out = []
        if any(c.startswith("FR-") and c not in dom for c in a1) or (not a1 and "FR" in region.countries):
            out.append("Metropole")
        out += [dom[c] for c in a1 if c in dom] + [ARCEP_DOM[c] for c in region.countries if c in ARCEP_DOM]
        return list(dict.fromkeys(out)) or ["Metropole"]

    def keys(self, prefix: str, http=None) -> list[tuple[str, int]]:
        import ogc_http as oh
        http = http or oh.SafeHTTP()
        out, token = [], None
        for _ in range(50):
            q = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token:
                q["continuation-token"] = token
            t = http.get(ARCEP_BUCKET, params=q, expect="any", max_bytes=10_000_000).text
            out += [(k, int(s)) for k, s in re.findall(r"<Key>([^<]+)</Key>.*?<Size>(\d+)</Size>", t, re.S)]
            m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", t)
            if not m:
                break
            token = m.group(1)
        return out

    def quarter(self, http=None) -> str:
        import ogc_http as oh
        http = http or oh.SafeHTTP()
        t = http.get(ARCEP_BUCKET, params={"list-type": "2", "prefix": ARCEP_PREFIX, "delimiter": "/"}, expect="any", max_bytes=2_000_000).text
        qs = sorted(set(re.findall(r"<Prefix>" + re.escape(ARCEP_PREFIX) + r"(\d{4}_T\d)/</Prefix>", t)))
        if not qs:
            raise AdapterError("the Arcep bucket lists no quarter of theoretical coverage", 5)
        return qs[-1]

    def listing(self, region, *, technology: str = "4G", use: str = "data", quarter: str = "latest", operators="all", http=None, **opts) -> list[dict]:
        q = self.quarter(http) if quarter in (None, "", "latest") else quarter
        terrs = self.territories(region)
        out = []
        for k, size in self.keys(f"{ARCEP_PREFIX}{q}/", http):
            m = ARCEP_FILE.search(k.rsplit("/", 1)[-1])
            if not m or m["terr"] not in terrs or m["tech"] != str(technology).upper().replace("LTE", "4G") or m["use"] != use:
                continue
            if operators not in (None, "all") and m["op"] not in operators and ARCEP_OPS.get(m["op"]) not in operators:
                continue
            out.append({"name": k.rsplit("/", 1)[-1], "url": ARCEP_BUCKET + k, "bytes": size, "operator": ARCEP_OPS.get(m["op"], m["op"])})
        if not out:
            raise AdapterError(f"Arcep lists no {technology} {use} file for {', '.join(terrs)} in {q}", 4)
        return out

    def local(self, folder: Path, region, technology: str = "4G", use: str = "data", **opts) -> list[Path]:
        terrs = self.territories(region)
        fs = [p for p in folder.glob("*_couv_*") if (m := ARCEP_FILE.search(p.name)) and m["terr"] in terrs
              and m["tech"] == str(technology).upper().replace("LTE", "4G") and m["use"] == use]
        if not fs:
            return []
        latest = max(ARCEP_FILE.search(p.name)["q"] for p in fs)
        return sorted(p for p in fs if ARCEP_FILE.search(p.name)["q"] == latest)

    def gpkg_of(self, p: Path) -> Path:
        out = p.with_suffix("") if p.name.endswith(".gpkg.7z") else p.with_suffix(".gpkg")
        if out.is_file() and out.stat().st_mtime >= p.stat().st_mtime:
            return out
        try:
            import py7zr
        except ImportError:
            raise AdapterError("reading Arcep's 7z files needs py7zr: doctor.py --fix --profile h3 installs it", 4) from None
        with py7zr.SevenZipFile(p) as z:
            names = [n for n in z.getnames() if n.lower().endswith(".gpkg")]
            z.extract(path=p.parent, targets=names)
        got = p.parent / names[0]
        if got != out:
            got.replace(out)
        return out

    def table(self, paths, region, technology: str = "4G", use: str = "data", level: str = "good", **opts):
        import geopandas as gpd
        import pyogrio
        levels = ARCEP_LEVELS.get(str(level), ARCEP_LEVELS["good"])
        cells, groups, steps = [], [], []
        for p in paths:
            m = ARCEP_FILE.search(p.name)
            g = self.gpkg_of(p)
            crs = pyogrio.read_info(g).get("crs") or "EPSG:2154"
            gdf = pyogrio.read_dataframe(g, bbox=bounds_in(region, crs))
            if "niveau" in gdf.columns and gdf["niveau"].notna().any():
                gdf = gdf[gdf["niveau"].isin(levels)]
            gdf = gpd.GeoDataFrame(gdf, crs=crs).to_crs(4326)
            cs = polys_to_cells(list(gdf.geometry), self.res, region.geometry)
            op = ARCEP_OPS.get(m["op"], m["op"])
            cells += cs
            groups += [op] * len(cs)
            steps.append(f"Arcep {m['q']} {m['tech']} {m['use']} coverage of {op} ({p.name}), levels {', '.join(levels)}: "
                         f"{sum(len(c) for c in cs):,} H3 cells at resolution {self.res}")
        return frame_of(cells, groups), steps


# ---------------------------------------------------------------------------------------------------------------- Australia
ACCC_PACKAGE = "https://data.gov.au/data/api/3/action/package_show?id=4b472a18-d0fa-409c-994a-ab17162bcb90"
ACCC_NAME = re.compile(r"^Coverage map - (?P<op>.+?) - (?P<tech>3G|4G|5G NSA|5G) - (?P<env>Outdoor & Ext Ant|Outdoor|Ext Ant)(?P<whole> - Wholesale)? - (?P<year>\d{4})$")
ACCC_OPS = ("Telstra", "Optus", "TPG")


class Accc(FileAdapter):
    name = "accc-mobile-coverage"
    entry_id = "ref.statistic.au.coverage.accc-mobile"
    coverage_countries = ("AU",)
    grid = "polygons"
    licence_class = "attribution"
    kind = name
    defaults = {"by": "provider", "agg": "count_groups"}

    def resources(self, http=None) -> list[dict]:
        import ogc_http as oh
        http = http or oh.SafeHTTP()
        d = json.loads(http.get(ACCC_PACKAGE, expect="any", max_bytes=20_000_000).text)
        return list((d.get("result") or {}).get("resources") or [])

    def listing(self, region, *, technology: str = "4G", environment: str = "outdoor", year="latest", operators=ACCC_OPS, http=None, **opts) -> list[dict]:
        env = {"outdoor": "Outdoor", "external-antenna": "Ext Ant", "ext-ant": "Ext Ant"}.get(str(environment).lower(), "Outdoor")
        tech = {"5GNR": "5G", "4GLTE": "4G", "LTE": "4G"}.get(str(technology).upper().replace(" ", ""), str(technology).upper())
        best: dict = {}
        for r in self.resources(http):
            m = ACCC_NAME.match(str(r.get("name") or "").strip())
            if not m or m["whole"] or m["tech"] != tech or m["env"] != env:
                continue
            if operators not in (None, "all") and m["op"] not in operators:
                continue
            if year not in (None, "", "latest") and m["year"] != str(year):
                continue
            if m["op"] not in best or m["year"] > best[m["op"]]["year"]:
                best[m["op"]] = {"name": str(r["url"]).rsplit("/", 1)[-1], "url": r["url"], "bytes": int(r.get("size") or 0), "operator": m["op"], "year": m["year"]}
        if not best:
            raise AdapterError(f"the ACCC data release lists no {tech} {env} coverage map" + (f" for {year}" if year not in (None, "", "latest") else ""), 4)
        return sorted(best.values(), key=lambda x: x["operator"])

    def local(self, folder: Path, region, technology: str = "4G", environment: str = "outdoor", **opts) -> list[Path]:
        tech = {"5GNR": "5g", "4GLTE": "4g", "LTE": "4g"}.get(str(technology).upper().replace(" ", ""), str(technology).lower())
        env = "ext-ant" if "ext" in str(environment).lower() else "outdoor"
        fs = [p for p in folder.glob("coverage-map-*") if re.search(rf"-{tech}-{env}-\d\d\d\d\.(kml|zip)$", p.name) and "wholesale" not in p.name]
        newest: dict = {}
        for p in fs:
            op = p.name.split("-")[2]
            if op == "optus" and p.name.startswith("coverage-map-optus-tpg"):
                continue
            yr = re.search(r"(\d{4})\.(?:kml|zip)$", p.name).group(1)
            if op not in newest or yr > newest[op][0]:
                newest[op] = (yr, p)
        return [v[1] for _k, v in sorted(newest.items())]

    def table(self, paths, region, **opts):
        import geopandas as gpd
        import pyogrio
        cells, groups, steps = [], [], []
        for p in paths:
            src = f"/vsizip/{p}" if p.suffix == ".zip" else str(p)
            if p.suffix == ".zip":
                import zipfile
                inner = [n for n in zipfile.ZipFile(p).namelist() if n.lower().endswith((".kml", ".shp", ".gpkg"))]
                src = f"/vsizip/{p}/{inner[0]}"
            gdf = gpd.GeoDataFrame(pyogrio.read_dataframe(src, bbox=region.bounds), crs=4326)
            op = {"telstra": "Telstra", "optus": "Optus", "tpg": "TPG"}.get(p.name.split("-")[2], p.name.split("-")[2])
            cs = polys_to_cells(list(gdf.geometry), self.res, region.geometry)
            cells += cs
            groups += [op] * len(cs)
            steps.append(f"ACCC coverage map of {op} ({p.name}): {sum(len(c) for c in cs):,} H3 cells at resolution {self.res} in {region.name}")
        return frame_of(cells, groups), steps


# ---------------------------------------------------------------------------------------------------------------- Canada
CANADA_LAYER = "https://maps-cartes.services.geo.ca/server_serveur/rest/services/NRCan/Wireless_Data_Network_Reseau_donnees_sans_fil/MapServer/0"


class CanadaWireless(FileAdapter):
    name = "canada-wireless-coverage"
    entry_id = "ref.statistic.ca.coverage.wireless-data-network"
    coverage_countries = ("CA",)
    grid = "polygons"
    licence_class = "open"
    kind = name
    defaults = {"by": "technology", "agg": "count_groups"}

    def listing(self, region, **opts) -> list[dict]:
        return [{"name": f"wireless-data-network_{region.id}.geojson", "url": CANADA_LAYER, "bytes": None, "service": True}]

    def fetch(self, region, folder: Path, *, confirm: bool = False, http=None, **opts) -> dict:
        import ogc_http as oh
        card = {"adapter": self.name, "region": region.name, "cost": {"files": 1, "bytes": None, "note": "a service query for the region's box"},
                "folder": str(folder), "licence": self.licence_class}
        if not confirm:
            return {"ok": True, "confirm_needed": True, "card": card}
        http = http or oh.SafeHTTP()
        W, S, E, N = region.bounds
        feats, offset = [], 0
        for _ in range(100):
            q = {"where": "1=1", "geometry": f"{W},{S},{E},{N}", "geometryType": "esriGeometryEnvelope", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
                 "outFields": "Speed,Year", "outSR": "4326", "f": "geojson", "resultOffset": str(offset), "resultRecordCount": "1000"}
            d = json.loads(http.get(CANADA_LAYER + "/query", params=q, expect="any", max_bytes=400_000_000).text)
            got = d.get("features") or []
            feats += got
            if len(got) < 1000 and not d.get("exceededTransferLimit"):
                break
            offset += len(got)
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / self.listing(region)[0]["name"]
        dest.write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
        return {"ok": True, "card": card, "files": [{"path": str(dest), "bytes": dest.stat().st_size, "features": len(feats)}]}

    def local(self, folder: Path, region, **opts) -> list[Path]:
        p = folder / f"wireless-data-network_{region.id}.geojson"
        return [p] if p.is_file() else sorted(folder.glob("wireless-data-network_*.geojson"))[-1:]

    def table(self, paths, region, technology: str = "all", **opts):
        import geopandas as gpd
        gdf = gpd.read_file(paths[0]).to_crs(4326)
        if "Year" in gdf.columns and len(gdf):
            gdf = gdf[gdf["Year"] == gdf["Year"].max()]
        if str(technology).lower() not in ("all", "", "none"):
            want = {"5GNR": "5G", "4GLTE": "LTE", "4G": "LTE"}.get(str(technology).upper(), str(technology).upper())
            gdf = gdf[gdf["Speed"].astype(str).str.upper() == want]
        cells, groups = [], []
        for sp, sub in gdf.groupby("Speed"):
            cs = polys_to_cells(list(sub.geometry), self.res, region.geometry)
            cells += cs
            groups += [str(sp)] * len(cs)
        df = frame_of(cells, groups, "technology")
        return df, [f"Atlas of Canada Wireless Data Network (CRTC data): LTE and 5G availability in {region.name}, {len(df):,} H3 cell-technology pairs "
                    "at resolution 9; not by provider"]


# ---------------------------------------------------------------------------------------------------------------- United Kingdom
OFCOM_PAGE = "https://www.ofcom.org.uk/phones-and-broadband/coverage-and-speeds/connected-nations-20252/data-downloads-2025"
ONS_LAD = "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/Local_Authority_Districts_December_2024_Boundaries_UK_BUC/FeatureServer/0"
OFCOM_API = "https://api-proxy.ofcom.org.uk/mobile/coverage/{pc}"


class Ofcom(FileAdapter):
    name = "ofcom-connected-nations"
    entry_id = "ref.statistic.uk.coverage.ofcom-connected-nations"
    coverage_countries = ("GB",)
    grid = "admin2"
    licence_class = "open"
    kind = name

    def listing(self, region, **opts) -> list[dict]:
        raise AdapterError("Ofcom's site refuses scripted downloads: download the Connected Nations 'Mobile local and unitary authority' zip in a "
                           f"browser from {OFCOM_PAGE} and put it (or its *_mobile_coverage_laua_*.csv) in data/{self.name}/", 4)

    def local(self, folder: Path, region, **opts) -> list[Path]:
        fs = sorted(folder.glob("*mobile*coverage*laua*.csv")) + sorted(folder.glob("*mobile*laua*.zip"))
        return fs[-1:]

    def read_csv(self, p: Path):
        import pandas as pd
        if p.suffix == ".zip":
            import zipfile
            z = zipfile.ZipFile(p)
            member = next(n for n in z.namelist() if n.lower().endswith(".csv") and "laua" in n.lower())
            with z.open(member) as f:
                return pd.read_csv(f)
        return pd.read_csv(p)

    def boundaries(self, region, folder: Path, http=None):
        import geopandas as gpd
        import ogc_http as oh
        cache = folder / f"ons_lad_dec2024_buc_{region.id}.geojson"
        if not cache.is_file():
            http = http or oh.SafeHTTP()
            W, S, E, N = region.bounds
            q = {"where": "1=1", "geometry": f"{W},{S},{E},{N}", "geometryType": "esriGeometryEnvelope", "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
                 "outFields": "LAD24CD,LAD24NM", "outSR": "4326", "f": "geojson", "resultRecordCount": "2000"}
            r = http.get(ONS_LAD + "/query", params=q, expect="any", max_bytes=200_000_000)
            folder.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(r.content)
        return gpd.read_file(cache).to_crs(4326)

    def fetch_layer(self, src: dict, base: Path, region=None, http=None):
        """`source: {type: adapter, adapter: ofcom-connected-nations, service: 4G, coverage: geo_out, min_operators: 3}` -> council polygons with
        `value` (the share, 0 to 100, covered by at least min_operators operators) and `operators_N` columns."""
        reg = region or region_now()
        folder = folder_for(src, base, self.name)
        paths = self.local(folder, reg) if folder.is_dir() else []
        if not paths:
            raise AdapterError(f"no Ofcom laua file in {folder.name}/. Download the Connected Nations 'Mobile local and unitary authority' zip in a browser "
                               f"from {OFCOM_PAGE} (Ofcom's site refuses scripted downloads) and put it there", 4)
        t = self.read_csv(paths[0])
        svc = {"5G": "5G_high_confidence", "5GNR": "5G_high_confidence", "4GLTE": "4G"}.get(str(src.get("service", "4G")).upper(), str(src.get("service", "4G")))
        ctype = str(src.get("coverage", "geo_out"))
        n_min = int(src.get("min_operators", 4))
        cols = [f"{svc}_{ctype}_{n}" for n in range(0, 5) if f"{svc}_{ctype}_{n}" in t.columns]
        if not cols:
            raise AdapterError(f"{paths[0].name} has no {svc}_{ctype}_N columns (services: 2G, 3G, 4G, 5G_high_confidence, 5G_very_high_confidence; "
                               "coverage: prem_out, prem_in, geo_out, abrd_in, mway_in, mway_ard_in)", 2)
        t["value"] = t[[c for c in cols if int(c.rsplit("_", 1)[1]) >= n_min]].sum(axis=1).round(1)
        lad = self.boundaries(reg, folder, http)
        g = lad.merge(t[["laua", "laua_name", "value"] + cols], left_on="LAD24CD", right_on="laua", how="inner")
        g = g[g.geometry.intersects(reg.geometry)]
        if not len(g):
            raise AdapterError(f"no council of {reg.name} matched the Ofcom file's laua codes", 4)
        steps = [f"Ofcom Connected Nations ({paths[0].name}): the share of {ctype.replace('_', ' ')} covered by {n_min} or more operators for {svc}, "
                 f"{len(g)} councils of {reg.name}, on ONS local authority boundaries (December 2024, BUC)"]
        return g, steps

    def fetch_h3(self, src: dict, base: Path, region_geom=None) -> dict:
        raise AdapterError("Ofcom publishes coverage by council, not on a grid (its 50 m predictions are not released): use "
                           "`source: {type: adapter, adapter: ofcom-connected-nations}` on a polygon layer instead of an H3 layer", 2)


def ofcom_postcodes(postcodes: list[str], *, auth=None, http=None, limit: int = 500) -> list[dict]:
    """Ofcom's mobile coverage API for a list of postcodes (the person's ofcom-mobile key, header Ocp-Apim-Subscription-Key). -> one row a
    postcode: the most common value of each operator's outdoor and indoor data and voice (0 none, 3 limited, 4 likely). At most `limit`
    postcodes a call (the basic plan allows 100 calls a minute and 50,000 a month)."""
    import collections
    import ogc_http as oh
    http = http or oh.SafeHTTP()
    out = []
    for pc in list(dict.fromkeys(re.sub(r"\s+", "", str(p)).upper() for p in postcodes if str(p).strip()))[:limit]:
        try:
            r = http.get(OFCOM_API.format(pc=urllib.parse.quote(pc)), expect="any", auth=auth, max_bytes=5_000_000)
            d = json.loads(r.text)
        except Exception as ex:  # noqa: BLE001
            out.append({"postcode": pc, "error": str(ex)[:120]})
            continue
        rows = [a for env in (d if isinstance(d, list) else [d]) for a in (env.get("Availability") or [])]
        rec = {"postcode": pc, "addresses": len(rows)}
        for op in ("EE", "H3", "TF", "VO"):
            for kind in ("DataOutdoor", "DataIndoor", "VoiceOutdoor", "VoiceIndoor"):
                vals = [a.get(f"{op}{kind}") for a in rows if a.get(f"{op}{kind}") is not None]
                rec[f"{op}{kind}"] = collections.Counter(vals).most_common(1)[0][0] if vals else None
        out.append(rec)
    return out


# ---------------------------------------------------------------------------------------------------------------- world
OCID_URL = "https://opencellid.org/downloads.php?type=mcc&file={mcc}.csv.gz"


def mcc_table() -> list[dict]:
    with open(gc.plugin_root() / "data" / "mcc-mnc.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def mccs_for(countries) -> list[str]:
    cs = {str(c).upper().split("-")[0] for c in countries}
    return sorted({r["mcc"] for r in mcc_table() if r["iso"] in cs})


def network_names() -> dict:
    """MCC-MNC -> network name. A network the table leaves unnamed keeps its code (the shipped table names no client of the maintainer)."""
    return {f"{r['mcc']}-{int(r['mnc'])}": r["network"] for r in mcc_table() if r["mnc"].isdigit() and r["network"]}


class OpenCellId(FileAdapter):
    name = "opencellid"
    entry_id = "ref.infrastructure.world.cell.opencellid"
    coverage_countries = None
    grid = "points"
    licence_class = "share-alike"
    credential = "opencellid"
    kind = name
    defaults = {"by": "provider", "agg": "count_groups"}

    def listing(self, region, **opts) -> list[dict]:
        ms = mccs_for(region.countries)
        if not ms:
            raise AdapterError(f"no mobile country code is known for {', '.join(region.countries)}", 4)
        return [{"name": f"{m}.csv.gz", "url": OCID_URL.format(mcc=m), "bytes": None, "mcc": m} for m in ms]

    def local(self, folder: Path, region, **opts) -> list[Path]:
        ms = mccs_for(region.countries)
        return [folder / f"{m}.csv.gz" for m in ms if (folder / f"{m}.csv.gz").is_file()]

    def table(self, paths, region, radio: str = "all", **opts):
        import pandas as pd
        from shapely import contains_xy
        W, S, E, N = region.bounds
        names = network_names()
        frames = []
        for p in paths:
            d = pd.read_csv(p, header=None, names=["radio", "mcc", "net", "area", "cell", "unit", "lon", "lat", "range", "samples", "changeable",
                                                  "created", "updated", "averageSignal"], compression="gzip", low_memory=False)
            if str(d.iloc[0]["radio"]).lower() == "radio":
                d = d.iloc[1:]
            d["lon"], d["lat"] = pd.to_numeric(d["lon"], errors="coerce"), pd.to_numeric(d["lat"], errors="coerce")
            d = d[(d["lon"] >= W) & (d["lon"] <= E) & (d["lat"] >= S) & (d["lat"] <= N)]
            if str(radio).lower() not in ("all", ""):
                d = d[d["radio"].astype(str).str.upper() == str(radio).upper()]
            frames.append(d)
        d = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if not len(d):
            raise AdapterError(f"no OpenCelliD cell lies inside {region.name}", 4)
        d = d[contains_xy(region.geometry, d["lon"].to_numpy(), d["lat"].to_numpy())]
        key = d["mcc"].astype(int).astype(str) + "-" + d["net"].astype(int).astype(str)
        df = pd.DataFrame({"_h3": points_to_cells(d["lon"].to_numpy(), d["lat"].to_numpy(), self.res), "provider": key.map(lambda k: names.get(k, k)).to_numpy()})
        return df, [f"OpenCelliD (CC BY-SA 4.0): {len(df):,} cells in {region.name} from {', '.join(p.name for p in paths)}, by network, on H3 resolution {self.res}; "
                    "crowd-sourced positions, not a site inventory"]


OOKLA_BUCKET = "https://ookla-open-data.s3.amazonaws.com/"
OOKLA_METRIC = {"download": "avg_d_kbps", "upload": "avg_u_kbps", "latency": "avg_lat_ms"}


class Ookla(FileAdapter):
    name = "ookla-open-data"
    entry_id = "ref.statistic.world.speed.ookla"
    coverage_countries = None
    grid = "quadkey"
    licence_class = "non-commercial"
    kind = name
    res = 8
    defaults = {"agg": "mean", "value_field": "value"}

    def listing(self, region, *, kind: str = "mobile", quarter: str = "latest", http=None, **opts) -> list[dict]:
        import ogc_http as oh
        http = http or oh.SafeHTTP()
        t = http.get(OOKLA_BUCKET, params={"list-type": "2", "prefix": f"parquet/performance/type={kind}/"}, expect="any", max_bytes=5_000_000).text
        keys = [(k, int(s)) for k, s in re.findall(r"<Key>([^<]+\.parquet)</Key>.*?<Size>(\d+)</Size>", t, re.S)]
        if quarter not in (None, "", "latest"):
            y, q = str(quarter).upper().split("Q") if "Q" in str(quarter).upper() else (None, None)
            keys = [k for k in keys if f"year={y}/quarter={q}/" in k[0]]
        if not keys:
            raise AdapterError(f"Ookla lists no {kind} tiles" + (f" for {quarter}" if quarter not in (None, "", "latest") else ""), 4)
        k, s = max(keys)
        return [{"name": k.rsplit("/", 1)[-1], "url": OOKLA_BUCKET + k, "bytes": s}]

    def local(self, folder: Path, region, kind: str = "mobile", **opts) -> list[Path]:
        return sorted(folder.glob(f"*_performance_{kind}_tiles.parquet"))[-1:]

    def table(self, paths, region, metric: str = "download", **opts):
        import duckdb
        import pandas as pd
        from shapely import contains_xy
        col = OOKLA_METRIC.get(str(metric), "avg_d_kbps")
        W, S, E, N = region.bounds
        con = duckdb.connect()
        d = con.execute(f"SELECT tile_x, tile_y, {col} AS v, tests FROM read_parquet(?) WHERE tile_x BETWEEN ? AND ? AND tile_y BETWEEN ? AND ?",
                        [str(paths[0]), W, E, S, N]).df()
        d = d[contains_xy(region.geometry, d["tile_x"].to_numpy(), d["tile_y"].to_numpy())]
        if not len(d):
            raise AdapterError(f"{paths[0].name} has no tile inside {region.name}", 4)
        v = d["v"] / 1000.0 if col != "avg_lat_ms" else d["v"]
        reps = np.clip(d["tests"].fillna(1).astype(int).to_numpy(), 1, 50)               # a tile counts by its tests (capped), so the mean is test-weighted
        df = pd.DataFrame({"_h3": np.repeat(points_to_cells(d["tile_x"].to_numpy(), d["tile_y"].to_numpy(), self.res), reps), "value": np.repeat(v.to_numpy(), reps)})
        unit = "ms" if col == "avg_lat_ms" else "Mbps"
        return df, [f"Ookla Speedtest open data ({paths[0].name}, CC BY-NC 4.0, internal use): {len(d):,} zoom-16 tiles in {region.name}, mean {metric} "
                    f"in {unit} weighted by tests (at most 50 a tile), on H3 resolution {self.res}"]


BNETZA = BNetzA()
ARCEP = Arcep()
ACCC = Accc()
CANADA = CanadaWireless()
OFCOM = Ofcom()
OPENCELLID = OpenCellId()
OOKLA = Ookla()
