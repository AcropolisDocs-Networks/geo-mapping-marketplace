"""Adapters that point at a tool rather than files (3.0.0, G-13): the Census tables (census_query) and the population grids (GHSL, WorldPop).
The coverage adapters outside the US fetch their own files since 3.1.0 (adapters/coverage.py)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from adapters import Adapter, AdapterError  # noqa: E402


class Candidate(Adapter):
    status = "candidate"
    to_verify = "terms and keyless access"

    def __init__(self, name, entry_id, countries, grid, licence_class, credential=None, to_verify=None, note=None, status="candidate"):
        self.name, self.entry_id, self.coverage_countries, self.grid = name, entry_id, countries, grid
        self.licence_class, self.credential, self.status = licence_class, credential, status
        self.to_verify = to_verify or self.to_verify
        self.note = note

    def fetch(self, parts: dict, **kw) -> dict:
        raise AdapterError(f"{self.name} is a candidate adapter: {self.to_verify} are verified before it fetches (spec global-regions G-12)", 4)

    fetch_h3 = None

    def describe(self, region=None) -> dict:
        d = super().describe(region)
        d.update(to_verify=self.to_verify if self.status == "candidate" else None, note=self.note)
        return d


CENSUS_ACS = Candidate("census-acs", "ref.statistic.us.demographics.acs5", ("US", "PR"), "polygons", "public-domain", credential="census", status="active",
                       note="US data: the census_query tool per county or tract; the region's us_states are its partitions")
POPULATION = Candidate("population-grid", "ref.statistic.world.population.ghsl", None, "raster", "attribution", status="active",
                       note="G-13: GHSL (CC BY 4.0) outside the United States for population covered and per-capita classes; the Census stays for the US; "
                            "WorldPop as the 100 m alternative")
