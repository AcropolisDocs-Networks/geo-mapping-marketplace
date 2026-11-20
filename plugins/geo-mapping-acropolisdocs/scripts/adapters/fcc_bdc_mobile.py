"""FCC BDC mobile coverage as an H3 adapter (spec global-regions H-11, H-12): US data, global approach.

The FCC publishes mobile coverage per state and provider as H3 resolution-9 hexagon files (zipped GeoPackages, Hexagon Coverage). This adapter
takes a region, lists the states it touches (region.partitions['us_states']), says which parts are outside the FCC's coverage (BC in
Cascadia), and reads the files already fetched into the work folder (data/fcc-bdc/) as one H3 table with provider, technology, speed,
environment and state columns. The fetch itself runs where the person's fcc-bdc key is: the MCP tool fcc_bdc_coverage(region, ...) on their
computer, with a cost card first.

Environment codes (`environmnt`): 0 outdoor stationary, 1 in-vehicle. Speed tiers in the file name: 7_1 (7/1 Mbps), 35_3 (35/3 Mbps).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from adapters import Adapter, AdapterError  # noqa: E402

ENVIRONMENT = {0: "outdoor", 1: "in-vehicle"}                      # H-12: named, never a code in a spec
ENVIRONMENT_LABEL = {"outdoor": "Outdoor stationary", "in-vehicle": "In-vehicle"}
SPEEDS = {"7/1": "7_1", "35/3": "35_3"}
TECH = {"5GNR": "5GNR", "5G": "5GNR", "5G-NR": "5GNR", "4GLTE": "4GLTE", "LTE": "4GLTE", "4G": "4GLTE", "3G": "3G"}
NAME = re.compile(r"bdc_(?P<fips>\d{2})_(?P<prov>\d+)_(?P<tech>[0-9A-Z]+)_(?P<speed>\d+_\d+)_mobile_broadband_h3_(?P<rest>.*)\.gpkg$", re.I)


def env_code(environment: str | int | None) -> int | None:
    if environment in (None, "", "all"):
        return None
    if isinstance(environment, int):
        return environment
    inv = {v: k for k, v in ENVIRONMENT.items()}
    e = str(environment).strip().lower().replace("_", "-")
    if e not in inv:
        raise AdapterError(f"environment '{environment}' is not one of outdoor, in-vehicle", 2)
    return inv[e]


class FccBdcMobile(Adapter):
    name = "fcc-bdc-mobile"
    entry_id = "ref.telecom.us.fcc-bdc.mobile-h3"
    coverage_countries = ("US", "PR", "VI", "GU", "AS", "MP")
    grid = "h3"
    licence_class = "open"
    credential = "fcc-bdc"

    def partitions(self, region) -> dict:
        import census_query as cq
        inside = [s for s in region.partitions.get("us_states") or []]
        outside = [c for c in region.admin1 if not c.startswith("US-")]
        return {"inside": inside, "fips": [cq.fips(s) for s in inside], "outside": outside}

    def files_for(self, folder: Path, parts: dict, technology: str = "5GNR", speed: str = "35/3") -> list[Path]:
        tech = TECH.get(str(technology).upper().replace(" ", ""), str(technology))
        sp = SPEEDS.get(str(speed), str(speed).replace("/", "_"))
        out = []
        for p in sorted(folder.rglob("*.gpkg")):
            m = NAME.search(p.name)
            if m and m["fips"] in set(parts.get("fips") or []) and m["tech"].upper() == tech.upper() and m["speed"] == sp:
                out.append(p)
        return out

    def fetch_h3(self, src: dict, base: Path, region_geom=None) -> dict:
        """The layer's H3 table from the files already in the work folder (data/fcc-bdc, or the source's path)."""
        import h3_source as hs
        import map_plan as mp
        reg = mp._BUILD.get("region")
        if reg is None:
            raise AdapterError("source adapter fcc-bdc-mobile needs a region: block (the states are read from it)", 2)
        parts = self.partitions(reg)
        if not parts["inside"]:
            raise AdapterError(f"the region {reg.name} is outside the FCC's coverage (the United States and its territories)", 2)
        folder = Path(src.get("path") or "data/fcc-bdc")
        folder = folder if folder.is_absolute() else base / folder
        files = self.files_for(folder, parts, src.get("technology", "5GNR"), src.get("speed", "35/3")) if folder.is_dir() else []
        have = {NAME.search(p.name)["fips"] for p in files}
        missing = [s for s, f in zip(parts["inside"], parts["fips"], strict=False) if f not in have]
        if missing:
            raise AdapterError(f"the FCC hexagon files for {', '.join(missing)} ({src.get('technology', '5GNR')}, {src.get('speed', '35/3')}) are not in "
                               f"{folder.name}/ yet. On your computer, run the map server's fcc_bdc_coverage tool for this region (it shows the "
                               "files and their size first, then fetches them with your key into the work folder), then build again", 4)
        code = env_code(src.get("environment", "outdoor"))
        where = f"environmnt = {code}" if code is not None else None
        df = hs.read_ids(files, "h3_res9_id", group_field="brandname", where=where)
        df["state"] = df["source_file"].map(lambda n: NAME.search(n)["fips"]) if "source_file" in df.columns else parts["fips"][0]
        df["environment"] = ENVIRONMENT_LABEL.get(ENVIRONMENT.get(code, ""), "all environments")
        df["provider"] = df["brandname"]                         # `h3: {by: provider}` reads the brand name
        df["technology"] = src.get("technology", "5GNR")
        df["speed"] = src.get("speed", "35/3")
        steps = [f"FCC BDC mobile hexagon coverage, {src.get('technology', '5GNR')} {src.get('speed', '35/3')} Mbps, "
                 f"{ENVIRONMENT_LABEL.get(ENVIRONMENT.get(code, ''), 'all environments')}: {len(files)} file(s) for {', '.join(parts['inside'])}"
                 + (f"; {', '.join(parts['outside'])} is outside the FCC's coverage and has no data" if parts["outside"] else "")]
        mp._BUILD.setdefault("adapter_parts", {})[self.name] = parts
        return {"table": df, "steps": steps, "partitions": parts}

    def cost(self, parts: dict, files: list[dict] | None = None, **kw) -> dict:
        fl = files or []
        return {"files": len(fl), "bytes": int(sum(int(f.get("bytes") or 0) for f in fl)), "states": parts.get("inside"), "outside": parts.get("outside")}


ADAPTER = FccBdcMobile()


def coverage_plan(client, region, *, technology: str = "5GNR", speed: str = "35/3", as_of: str = "latest", providers="all") -> dict:
    """H-11: the hexagon files a region needs, one list across its states (no download): -> {as_of, files, cost, partitions}."""
    import fcc_bdc as fb
    parts = ADAPTER.partitions(region)
    if as_of in (None, "", "latest"):
        ds = sorted(d["as_of_date"] for d in fb.dates(client, "availability"))
        if not ds:
            raise AdapterError("the FCC lists no availability dates", 5)
        as_of = ds[-1]
    tech = TECH.get(str(technology).upper().replace(" ", ""), str(technology))
    sp = SPEEDS.get(str(speed), str(speed).replace("/", "_"))
    want = []
    for st in parts["inside"]:
        for f in fb.list_files(client, as_of, category="Provider", subcategory="Hexagon Coverage", technology="Mobile Broadband", state=st):
            nm = str(f.get("name") or "")
            if f"_{tech}_".upper() not in nm.upper() or f"_{sp}_" not in nm:
                continue
            if providers not in (None, "all") and not any(str(p).lower() in str(f.get("provider") or "").lower() for p in providers):
                continue
            want.append({**f, "state": st})
    return {"as_of": as_of, "files": want, "cost": ADAPTER.cost(parts, want), "partitions": parts}
