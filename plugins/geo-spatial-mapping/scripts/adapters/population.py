"""Population adapters on H3 (3.1.0).

    kontur-population   World, by country. Kontur Population: people per H3 cell at resolution 8 (about 0.74 km2), from GHSL, Meta's HRSL,
                        Microsoft and OSM buildings and national censuses, one GeoPackage (gzip) per country on Kontur's public bucket and
                        HDX. CC BY 4.0, credit "Kontur Population, Kontur (kontur.io), CC BY 4.0". Already a hex grid, so the map's h3 block
                        rolls it up as it is (sum of people), or a coarser resolution sums the children.

The files are found through each country's HDX dataset (data.humdata.org; the bucket itself refuses listings), the newest release a country, fetched into data/kontur-population/ with a cost card
first (adapters_cli.py fetch kontur-population --region ... or the map server's coverage_fetch tool), and read for the region at build time.
The world file (2.4 GB) is never fetched: a region names its countries.
"""
from __future__ import annotations

import gzip
import json
import re
import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from adapters import AdapterError  # noqa: E402
from adapters.coverage import FileAdapter  # noqa: E402

KONTUR_BUCKET = "https://geodata-eu-central-1-kontur-public.s3.amazonaws.com/"
KONTUR_PREFIX = "kontur_datasets/kontur_population_"
KONTUR_FILE = re.compile(r"kontur_datasets/kontur_population_([A-Z]{2})_(\d{8})\.gpkg\.gz")
HDX_SEARCH = "https://data.humdata.org/api/3/action/package_search"


class KonturPopulation(FileAdapter):
    name = "kontur-population"
    entry_id = "ref.statistic.world.population.kontur"
    coverage_countries = None
    grid = "h3"
    licence_class = "attribution"
    kind = name
    res = 8
    defaults = {"agg": "sum", "value_field": "population"}

    def hdx_files(self, iso3: str, http=None) -> list[tuple[str, str, int]]:
        """[(date, url, bytes)] of Kontur's population files for one country, from its HDX dataset (the bucket refuses listings)."""
        import ogc_http as oh
        http = http or oh.SafeHTTP()
        r = http.get(HDX_SEARCH, params={"fq": f"organization:kontur groups:{iso3.lower()}", "q": "name:kontur-population*", "rows": "5"},
                     expect="json", max_bytes=5_000_000)
        out = []
        for pkg in (json.loads(r.content).get("result") or {}).get("results") or []:
            if not str(pkg.get("name", "")).startswith("kontur-population-"):
                continue
            for res in pkg.get("resources") or []:
                m = KONTUR_FILE.search(str(res.get("url") or ""))
                if m and str(res["url"]).startswith(KONTUR_BUCKET):
                    out.append((m.group(2), KONTUR_BUCKET + m.group(0), int(res.get("size") or 0)))
        return out

    def countries(self, region) -> list[str]:
        """The countries holding a real part of the region (a border sliver does not fetch a neighbour's whole file)."""
        import region as region_mod
        cs = sorted({str(c).upper().split("-")[0] for c in region.countries})
        if len(cs) <= 1:
            return cs
        try:
            from shapely.ops import unary_union
            a1 = region_mod._basemap(region_mod.ADMIN1)
            g = region.geometry
            # each country dissolved and drawn 1 km in, so the generalised borders' overlap (6 percent of Liechtenstein) is not a share
            share = {c: unary_union(list(a1[a1["iso_a2"] == c].geometry)).buffer(-0.01).intersection(g).area / g.area for c in cs} if g.area else {}
            top = max(share.values()) if share else 0.0
            keep = [c for c in cs if share.get(c, 0.0) >= max(0.01, top * 0.1)]
            return keep or cs
        except Exception:  # noqa: BLE001
            return cs

    def listing(self, region, *, http=None, **opts) -> list[dict]:
        import region as region_mod
        files = []
        for iso2 in self.countries(region):
            iso3 = region_mod.iso3_of(iso2)
            got = self.hdx_files(iso3, http) if iso3 else []
            if not got:
                raise AdapterError(f"Kontur publishes no population file for {iso2} on HDX", 4)
            date, url, size = max(got)
            files.append({"name": url.rsplit("/", 1)[-1], "url": url, "bytes": size, "country": iso2})
        return files

    def local(self, folder: Path, region, **opts) -> list[Path]:
        out = []
        for c in self.countries(region):
            got = sorted(folder.glob(f"kontur_population_{c}_*.gpkg.gz")) + sorted(folder.glob(f"kontur_population_{c}_*.gpkg"))
            if got:
                out.append(got[-1])
        return out

    def gpkg_of(self, p: Path) -> Path:
        if p.suffix != ".gz":
            return p
        q = p.with_suffix("")
        if not q.is_file() or q.stat().st_mtime < p.stat().st_mtime:
            with gzip.open(p, "rb") as src, open(q.with_suffix(".part"), "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            q.with_suffix(".part").replace(q)
        return q

    def table(self, paths, region, **opts):
        import h3
        import pandas as pd
        import pyogrio
        from shapely import contains_xy
        frames = []
        W, S, E, N = region.bounds
        for p in paths:
            g = self.gpkg_of(p)
            d = pyogrio.read_dataframe(g, columns=["h3", "population"], read_geometry=False)
            frames.append(d)
        d = pd.concat(frames, ignore_index=True)
        ids = d["h3"].astype(str).to_numpy()
        lat_lon = np.array([h3.cell_to_latlng(c) for c in ids])
        lat, lon = lat_lon[:, 0], lat_lon[:, 1]
        keep = (lon >= W) & (lon <= E) & (lat >= S) & (lat <= N)
        keep[keep] = contains_xy(region.geometry, lon[keep], lat[keep])
        if not keep.any():
            raise AdapterError(f"no Kontur cell lies inside {region.name}", 4)
        df = pd.DataFrame({"_h3": np.fromiter((int(c, 16) for c in ids[keep]), dtype=np.uint64, count=int(keep.sum())),
                           "population": pd.to_numeric(d["population"], errors="coerce").to_numpy()[keep]})
        return df, [f"Kontur Population (CC BY 4.0, {', '.join(p.name for p in paths)}): {len(df):,} H3 resolution-8 cells in {region.name}, "
                    f"{df['population'].sum():,.0f} people; a modelled estimate from buildings and censuses, not a count"]


KONTUR = KonturPopulation()
