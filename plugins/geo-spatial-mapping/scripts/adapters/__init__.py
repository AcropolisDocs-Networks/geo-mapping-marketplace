"""Data adapters (3.0.0, spec global-regions G-11): one interface for every source that is published by partition (per state, per county, per
NUTS area, per tile), so the build and the skills take a region and never a state code.

    adapter = adapters.get("fcc-bdc-mobile")
    adapter.covers(region)      -> share of the region the source covers (0 to 1)
    adapter.partitions(region)  -> the partitions the region touches (["WA", "OR", "ID"]) and the ones outside the source (["CA-BC"])
    adapter.cost(parts, ...)    -> files and bytes, for a cost card before a multi-partition fetch (R6)
    adapter.fetch(parts, ...)   -> the fetched files (on the computer where the credential lives)
    adapter.merge(paths, ...)   -> one table, every partition in it
    adapter.provenance()        -> the library entry and the partitions used
    adapter.entry()             -> the library entry the layer is credited by

`catalog_search` and `geo-find-data` rank adapters by covers(region): a US source appears when the region is in the US, like any national source.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REGISTRY = {"fcc-bdc-mobile": "adapters.fcc_bdc_mobile:ADAPTER", "census-acs": "adapters.catalog:CENSUS_ACS", "opencellid": "adapters.coverage:OPENCELLID",
            "ookla-open-data": "adapters.coverage:OOKLA", "ofcom-connected-nations": "adapters.coverage:OFCOM", "arcep-mon-reseau-mobile": "adapters.coverage:ARCEP",
            "bnetza-mobile-coverage": "adapters.coverage:BNETZA", "canada-wireless-coverage": "adapters.coverage:CANADA", "accc-mobile-coverage": "adapters.coverage:ACCC",
            "population-grid": "adapters.catalog:POPULATION", "kontur-population": "adapters.population:KONTUR"}
ALIASES = {"ised-mobile-coverage": "canada-wireless-coverage"}          # 3.0.0's candidate name


class AdapterError(Exception):
    def __init__(self, message: str, code: int = 4, rule: str = "MAP-REG-003"):
        super().__init__(message)
        self.code = code
        self.rule = rule


class Adapter:
    """The interface. A subclass sets `name`, `entry_id`, `coverage_countries` (ISO alpha-2, or None for the world), `grid` (h3, quadkey,
    squares, polygons), `licence_class` (open, attribution, share-alike, non-commercial) and `credential` (None when keyless)."""
    name = "adapter"
    entry_id: str | None = None
    coverage_countries: tuple | None = None
    grid = "polygons"
    licence_class = "open"
    credential: str | None = None
    status = "active"

    def covers(self, region) -> float:
        if self.coverage_countries is None:
            return 1.0
        from shapely.ops import unary_union
        import region as region_mod
        a1 = region_mod._basemap(region_mod.ADMIN1)
        mine = a1[a1["iso_a2"].isin(self.coverage_countries)]
        if not len(mine):
            return 0.0
        shape = unary_union(list(mine.geometry))
        g = region.geometry
        return float(shape.intersection(g).area / g.area) if g.area else 0.0

    def partitions(self, region) -> dict:
        return {"inside": list(region.countries), "outside": []}

    def cost(self, parts: dict, **kw) -> dict:
        return {"files": None, "bytes": None, "note": "the source does not say its size before a fetch"}

    def fetch(self, parts: dict, **kw) -> dict:
        raise AdapterError(f"{self.name}: fetching is not built for this adapter yet")

    def merge(self, paths: list, **kw):
        raise AdapterError(f"{self.name}: merging is not built for this adapter yet")

    def entry(self) -> dict | None:
        if not self.entry_id:
            return None
        try:
            import map_plan as mp
            return mp.library_entry(self.entry_id)
        except Exception:  # noqa: BLE001
            return None

    def provenance(self, parts: dict | None = None) -> dict:
        return {"adapter": self.name, "entry": self.entry_id, "partitions": parts or {}, "licence_class": self.licence_class}

    def describe(self, region=None) -> dict:
        d = {"name": self.name, "entry": self.entry_id, "grid": self.grid, "licence_class": self.licence_class, "credential": self.credential,
             "status": self.status, "countries": list(self.coverage_countries) if self.coverage_countries else "world"}
        if region is not None:
            d["covers"] = round(self.covers(region), 3)
        return d


def get(name: str) -> Adapter:
    mod = REGISTRY.get(ALIASES.get(str(name), str(name)))
    if mod is None:
        raise AdapterError(f"no adapter '{name}' (adapters: {', '.join(REGISTRY)})", 2)
    m, _, attr = mod.partition(":")
    return getattr(importlib.import_module(m), attr or "ADAPTER")


def blocked_for(adapter: "Adapter", audience: str) -> str | None:
    """R4 (owner decision O5): a non-commercial source is for an internal map only; an external audience is refused by licence class."""
    if adapter.licence_class == "non-commercial" and str(audience or "external").lower() != "internal":
        return f"{adapter.name} is licensed for non-commercial use only; it can be used with audience: internal, never on an external map"
    return None


def ranked(region) -> list[dict]:
    """Every adapter by how much of the region it covers (G-11: catalog_search and geo-find-data list adapters before a web search)."""
    out = []
    for n in REGISTRY:
        try:
            out.append(get(n).describe(region))
        except Exception as ex:  # noqa: BLE001
            out.append({"name": n, "status": f"unavailable ({type(ex).__name__})", "covers": 0.0})
    return sorted(out, key=lambda d: (-(1.0 if float(d.get("covers") or 0) >= 0.95 else float(d.get("covers") or 0)), d.get("countries") == "world"))   # a national source before a world one
