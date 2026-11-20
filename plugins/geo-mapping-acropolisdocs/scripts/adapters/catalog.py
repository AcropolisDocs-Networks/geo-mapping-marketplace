"""The adapters beyond the FCC (3.0.0, spec global-regions G-12, G-13), each with its grid, licence class and coverage declared. Their terms and
access are verified one by one before they fetch: until then `status: candidate` and fetch() says what is left to verify. The interface and the
ranking (`catalog_search`, `geo-find-data`) already see them, so a UK region is offered Ofcom like a US region is offered the FCC."""
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
OPENCELLID = Candidate("opencellid", None, None, "points", "share-alike", credential="opencellid",
                       to_verify="the CC BY-SA 4.0 attribution text and the bulk-download terms for a key holder",
                       note="cell locations, worldwide; points go on H3 with hexbin")
OOKLA = Candidate("ookla-open-data", None, None, "quadkey", "non-commercial",
                  to_verify="CC BY-NC-SA 4.0: internal maps only (owner decision O5); the quadkey to H3 roll-up by area share",
                  note="performance tiles (zoom 16 quadkeys), quarterly")
OFCOM = Candidate("ofcom-connected-nations", None, ("GB",), "squares", "open", to_verify="the Open Government Licence wording and the 100 m grid files",
                  note="UK mobile coverage by operator on 100 m squares; squares go on H3 by area share")
ARCEP = Candidate("arcep-mon-reseau-mobile", None, ("FR",), "polygons", "open", to_verify="Licence Ouverte 2.0 and the coverage polygons per operator")
BNETZA = Candidate("bnetza-mobile-coverage", None, ("DE",), "squares", "open", to_verify="the dl-de licence and the 100 m grid download")
ISED = Candidate("ised-mobile-coverage", None, ("CA",), "polygons", "open", to_verify="the Open Government Licence - Canada and the provider footprints")
ACCC = Candidate("accc-mobile-coverage", None, ("AU",), "polygons", "open", to_verify="the ACCC/ACMA terms for the published coverage")
POPULATION = Candidate("population-grid", "ref.statistic.world.population.ghsl", None, "raster", "attribution", status="active",
                       note="G-13: GHSL (CC BY 4.0) outside the United States for population covered and per-capita classes; the Census stays for the US; "
                            "WorldPop as the 100 m alternative")
