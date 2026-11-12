#!/usr/bin/env python3
"""telecom_maps: turn a site list, a market table or a program tracker into a MapSpec the build can draw (spec 7.2: coverage-map, market-map, program-map).

    telecom_maps.py coverage --sites sites.csv --out DIR [--option key=value ...]
    telecom_maps.py market   --table markets.xlsx --out DIR --value subs [--denominator pop]
    telecom_maps.py program  --tracker tracker.xlsx --out DIR [--coordinates venues.csv]
    telecom_maps.py network  --sites sites.csv --out DIR --option analysis=spacing,best-server [--option links=... neighbors=... samples=... kpi=... population=...]

Each writes `DIR/map.yaml` plus the cleaned data it points at, and prints what it found (JSON). The pipelines (`/map-run coverage-map`, `market-map`, `program-map`)
call these and then build the map with every lint; run alone they are a way to look at the MapSpec before building.

Rules that hold for all three
  * The person's tables are data, not content: nothing is looked up, geocoded or guessed. A row without coordinates is listed and left off the map.
  * Client tables default to client-confidential (banner on, privacy options). `confidential: false` turns that off, for data that is public.
  * Every map needs a message; one is written from the data when none is given, and says only what the data shows.
  * Colors are brand tokens only; statuses and categories use shape or label as well as color.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import classify as cf  # noqa: E402
import site_privacy as sp  # noqa: E402
import table_join as tj  # noqa: E402
import telecom_geo as tg  # noqa: E402

KEY_NAMES = ("geoid", "fips", "county_fips", "state_fips", "fips_code", "geoid10", "geoid20", "stfips", "cnty_fips", "county_geoid", "state", "st", "state_code", "state_abbr",
             "state_name", "county", "market", "name")
DEFAULT_PER = 1000
TEMPLATE = "letter-landscape"


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


class PrepError(ValueError):
    def __init__(self, message: str, fid: str = "MAP-RUN-002"):
        super().__init__(message)
        self.fid = fid


def today() -> str:
    return os.environ.get("GMAP_FIXED_DATE") or dt.date.today().isoformat()


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "map"


def _norm(c) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_")


def _nice(text: str) -> str:
    """A column name made readable for a legend or title: underscores to spaces, first letter capital, the rest left alone."""
    t = re.sub(r"[_]+", " ", str(text)).strip()
    return t[:1].upper() + t[1:]


PER_WORDS = {"pop": "people", "population": "people", "total population": "people", "pop total": "people", "persons": "people", "residents": "people",
             "hh": "households", "households": "households", "housing units": "housing units", "hu": "housing units"}


def _per_word(den: str) -> str:
    """The denominator as the legend says it (TODO M6): a population column is "people", a household column "households"; a year or an
    estimate word is dropped (pop_2020_est is people); anything else is the column made readable, in lower case."""
    t = re.sub(r"[_]+", " ", str(den)).strip().lower()
    t = re.sub(r"\b(?:19|20)\d{2}\b|\b(?:est|estimate|acs)\b", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return PER_WORDS.get(t) or t or str(den)


def _num(x: float) -> str:
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,g}"


def pick_column(columns, wanted: str | None, names=(), what: str = "column"):
    """The column named `wanted` (any case), or the first of `names` present; None when nothing fits."""
    by = {_norm(c): c for c in columns}
    if wanted:
        if _norm(wanted) in by:
            return by[_norm(wanted)]
        raise PrepError(f"there is no {what} '{wanted}'; columns are: {', '.join(map(str, columns))}")
    for n in names:
        if n in by:
            return by[n]
    return None


def base_spec(map_id: str, message: str, title: str, subtitle: str | None, opt: dict, bbox=None) -> dict:
    prov, why = pick_basemap(opt, bbox)
    spec = {
        "mapspec_version": 1, "id": map_id, "template": opt.get("template") or TEMPLATE, "audience": opt.get("audience") or "external",
        "message": message, "extent": {"mode": "fit_layers", "padding_pct": 6}, "basemap": {"provider": prov},
        "layers": [],
        "elements": {"title": {"on": True, "text": title, **({"subtitle": subtitle} if subtitle else {})}},
    }
    if opt.get("brand"):
        spec["brand"] = opt["brand"]
    if opt.get("formats"):
        spec["export"] = {"formats": list(opt["formats"])}
    return spec


def provenance(source: str, license_: str, date: str | None = None, **kw) -> dict:
    return {"source": source, "license": license_, "retrieved_at": date or today(), **kw}


def pick_basemap(opt: dict, bbox) -> tuple[str, str | None]:
    """The bundled basemap is generalized for scales smaller than 1:1,000,000. A city-scale map would draw misleading boundaries, so it gets none (a plain background)."""
    if opt.get("basemap"):
        return str(opt["basemap"]), None
    if bbox:
        import math
        x0, y0, x1, y1 = bbox
        km = max(abs(x1 - x0) * 111.32 * math.cos(math.radians((y0 + y1) / 2)), abs(y1 - y0) * 110.57, 1e-6)
        denom = km * 1e6 / 250.0                    # about 250 mm of map across a letter page
        if denom < 800_000:
            return "none", f"no basemap: at about 1:{int(denom):,} the bundled boundaries are too generalized; prepare tiles with basemap-render or add a detailed boundary layer"
    return "bundled", None


class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data):             # a MapSpec read by people has no &anchors
        return True


DATUM_NOTE = "Positions are WGS84 (GPS) coordinates drawn on a NAD83 projection; the difference, about 1 m, is far below what this map can show."


def write_spec(spec: dict, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    p = out / "map.yaml"
    p.write_text(yaml.dump(spec, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=140))
    return p


def _confidential(opt: dict) -> bool:
    return opt.get("confidential") is not False


def _privacy(opt: dict, seed: int) -> dict | None:
    m = opt.get("privacy_m")
    if not m:
        return None
    return {"method": opt.get("privacy_method") or "precision", "meters": float(m), "seed": seed}


# ---------------------------------------------------------------------------------------------------------------- coverage-map
def prepare_coverage(inp: dict, opt: dict, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    # a site list whose every row is swapped raises tg.SiteListError("...", "MAP-TEL-012"); it is a ValueError with a fid, so the
    # pipeline and the CLI already report it as a MAP-TEL-012 BLOCK. No finding of that severity ever reaches `fs`, so none is checked here.
    df, fs, rep = tg.ingest_sites(inp["sites"], opt.get("sheet"), columns=opt.get("columns"), layer=opt.get("layer"))
    if opt.get("clip_bbox"):
        x0, y0, x1, y1 = [float(v) for v in opt["clip_bbox"]]
        keep = df["lon"].between(x0, x1) & df["lat"].between(y0, y1)
        if not keep.any():
            raise PrepError("no site falls inside clip_bbox", "MAP-TEL-019")
        if (~keep).any():
            fs.append(finding("MAP-TEL-019", "INFO", f"{int((~keep).sum())} site row(s) outside clip_bbox were left out"))
        df = df[keep].reset_index(drop=True)
    color_by = opt.get("color_by") or next((k for k in ("band", "tech", "site_type") if k in df.columns and df[k].notna().any()), None)
    if color_by and color_by not in df.columns:
        raise PrepError(f"color_by '{color_by}' is not a column of the site list (found: {', '.join(df.columns)})")
    wedges = opt.get("wedges", "azimuth" in df.columns and df["azimuth"].notna().any()) is not False and "azimuth" in df.columns
    sites_csv = out / "sites.csv"
    n_sites = int(df["site_id"].nunique()) if "site_id" in df.columns else int(len(df))
    if "site_id" in df.columns:                                     # one site, several sectors: keep the site name, give each row its own id
        df = df.rename(columns={"site_id": "site"})
        df["sector_id"] = df["site"].astype(str) + "-" + (df.groupby("site").cumcount() + 1).astype(str)
    df.to_csv(sites_csv, index=False)
    label = {"band": "band", "tech": "technology", "site_type": "site type"}.get(color_by or "", "")
    seed = opt.get("privacy_seed") if opt.get("privacy_seed") is not None else sp.default_seed("sites")
    priv = _privacy(opt, int(seed))
    conf = "client-confidential" if _confidential(opt) else "public"
    if "confidential" not in opt:
        fs.append(finding("MAP-TEL-019", "INFO", "the site list is treated as client-confidential (banner on, privacy options); for public or sample "
                          "sites pass confidential=false"))
    prov = provenance(opt.get("source") or f"{Path(inp['sites']).name} (site list supplied by the client)", opt.get("license") or ("client-proprietary" if _confidential(opt) else "public-domain"),
                      opt.get("date"))
    src = {"type": "file", "path": "sites.csv", "x": "lon", "y": "lat", "crs": "EPSG:4326"}
    spec = base_spec(opt.get("id") or f"{_slug(opt.get('title') or 'sector-map')}",
                     opt.get("message") or (f"{len(df)} sectors at {n_sites} sites, shown by {label or 'direction'}." if wedges else f"{n_sites} sites{', shown by ' + label if label else ''}."),
                     opt.get("title") or ("Sectors by " + label if (wedges and label) else "Sites"), opt.get("subtitle"), opt,
                     bbox=(float(df["lon"].min()), float(df["lat"].min()), float(df["lon"].max()), float(df["lat"].max())))
    nb = pick_basemap(opt, (float(df["lon"].min()), float(df["lat"].min()), float(df["lon"].max()), float(df["lat"].max())))[1]
    if nb:
        fs.append(finding("MAP-TEL-050", "INFO", nb))
    order = tg.canonical_order(df[color_by], color_by) if color_by else []
    cls = {"method": "categorical", "order": order, "max_categories": 8} if color_by else None
    spec["crs"] = {"display": "auto", "datum_note": DATUM_NOTE}
    if opt.get("coverage"):
        cov = Path(opt["coverage"])
        dst = out / ("availability" + cov.suffix.lower())
        dst.write_bytes(cov.read_bytes())
        spec["layers"].append({"id": "availability", "source": {"type": "file", "path": dst.name}, "role": "polygons",
                               "symbol": {"fill": "@brand.categorical.4", "opacity": 0.35, "stroke": "@brand.frame"}, "legend": {"title": opt.get("coverage_label") or "Reported availability"},
                               "provenance": provenance(opt.get("coverage_source") or f"{cov.name} (supplied)", opt.get("coverage_license") or "unknown", opt.get("coverage_date"))})
    if wedges:
        w = {"azimuth": "azimuth", "radius_m": float(opt.get("radius_m") or tg.DEFAULT_RADIUS_M)}
        if opt.get("radius_field") or "radius_m" in df.columns:                                   # 0.25.0 (TODO M6): a radius per sector
            w["radius_field"] = str(opt.get("radius_field") or "radius_m")
        if "beamwidth" in df.columns:
            w["beamwidth"] = "beamwidth"
        elif opt.get("beamwidth"):
            w["beamwidth"] = float(opt["beamwidth"])
        layer = {"id": "sectors", "source": src, "role": "polygons", "wedges": w, "confidentiality": conf, "provenance": prov,
                 "symbol": {"opacity": 0.85, "stroke": "@brand.frame", "stroke_width": 0.4}, "legend": {"title": ("Sectors by " + label) if label else "Sectors"}}
        if color_by:
            layer["value_field"], layer["classification"] = color_by, cls
        if priv:
            layer["privacy"] = priv
        spec["layers"].append(layer)
    else:
        layer = {"id": "sites", "source": src, "role": "points", "confidentiality": conf, "provenance": prov, "symbol": {"shape": "circle", "size": 5, "color": "@brand.accent"},
                 "legend": {"title": ("Sites by " + label) if label else "Sites"}}
        if color_by:
            layer["value_field"], layer["classification"] = color_by, cls
        if priv:
            layer["privacy"] = priv
        spec["layers"].append(layer)
    if wedges and opt.get("show_sites", True):
        pts = {"id": "sites", "source": src, "role": "points", "confidentiality": conf, "provenance": prov, "symbol": {"shape": "circle", "size": 2.2, "color": "@brand.label"},
               "legend": {"title": "Site", "label": "Site"}}
        if priv:
            pts["privacy"] = priv
        spec["layers"].append(pts)
    p = write_spec(spec, out)
    return {"spec": str(p), "findings": fs, "report": {**rep, "color_by": color_by, "wedges": bool(wedges), "sites": n_sites, "sectors": int(len(df))}, "files": [str(sites_csv)]}


# ---------------------------------------------------------------------------------------------------------------- market-map
MARKET_REFS = {"state": "ref.boundary.us.state.cb20m", "county": "ref.boundary.us.county.cb", "tract": "ref.boundary.us.tract.cb500k"}
MARKET_FILE_KEYS = {"cma": "CMA"}                   # the key column of a boundary file of these areas, when the person gives none


def _infer_geography(keys) -> tuple[str, str]:
    """('state'|'county', boundary column) from how the table's keys look."""
    ks = [tj.norm_key(k) for k in keys]
    ks = [k for k in ks if k]
    if not ks:
        raise PrepError("the key column is empty")
    if all(k.isdigit() for k in ks):
        m = max(len(k) for k in ks)
        if m == 11:                                       # 1.0.0 (TODO M6): state + county + tract
            return "tract", "GEOID"
        return ("state", "GEOID") if m <= 2 else ("county", "GEOID")
    if all(re.fullmatch(r"[A-Za-z]{2}", k) for k in ks):
        return "state", "STUSPS"
    return "state", "NAME"


def prepare_market(inp: dict, opt: dict, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    table = tj.read_table(inp["table"], opt.get("sheet"))
    fs: list[dict] = []
    key = pick_column(table.columns, opt.get("key"), KEY_NAMES, "key column")
    if not key:
        raise PrepError(f"no key column found in the table (columns: {', '.join(table.columns)}); say which one holds the county or state code with key=")
    val = pick_column(table.columns, opt.get("value"), (), "value column")
    if not val:
        nums = [c for c in table.columns if c != key and tj._to_number(table[c]).dtype.kind in "if"]
        if len(nums) == 1:
            val = nums[0]
        else:
            raise PrepError(f"say which column to map with value= (numeric columns: {', '.join(nums) or 'none'})")
    den = pick_column(table.columns, opt.get("denominator"), (), "denominator column") if opt.get("denominator") else None
    geography = opt.get("geography") or _infer_geography(table[key])[0]
    left = opt.get("boundary_key") or _infer_geography(table[key])[1]
    if geography in ("county", "tract") and left != "GEOID" and not opt.get("boundary"):
        raise PrepError(f"a {geography} table needs {'5' if geography == 'county' else '11'}-digit FIPS codes (GEOID); names alone are ambiguous across states")
    ref = MARKET_REFS.get(geography)
    bfile = Path(opt["boundary"]) if opt.get("boundary") else None
    if bfile is not None:                                # 1.0.0 (TODO M6): any areas from the person's file or a captured layer (CMAs: key CMA)
        if not bfile.is_file():
            raise PrepError(f"boundary file {bfile.name} was not found")
        left = opt.get("boundary_key") or MARKET_FILE_KEYS.get(geography) or left
    elif not ref:
        raise PrepError(f"geography '{geography}' has no bundled boundaries; use state, county or tract, or give boundary=FILE "
                        "(for CMAs, the FCC's CMA layer captured with service_query.py, key CMA)")
    style = opt.get("style") or "choropleth"
    if style not in ("choropleth", "proportional"):
        raise PrepError("style is choropleth or proportional")
    clean = table[[key]].copy()
    clean.columns = ["key"]
    clean["value"] = tj._to_number(table[val])
    extra = [c for c in (opt.get("extra_columns") or []) if c in table.columns and c not in (key, val)]
    if den:
        clean["denominator"] = tj._to_number(table[den])
        bad = (clean["denominator"].fillna(0) <= 0)
        if bad.any():
            fs.append(finding("MAP-JOIN-007", "WARN", f"{int(bad.sum())} row(s) have a zero or missing '{den}'; their rate is left blank"))
        per = float(opt.get("per") or DEFAULT_PER)
        clean["rate"] = (clean["value"] / clean["denominator"].where(clean["denominator"] > 0)) * per
        field_ = "rate"
        per_s = _num(per)
        legend = opt.get("legend") or f"{_nice(val)} per {per_s} {_per_word(den)}"
    else:
        field_ = "value"
        legend = opt.get("legend") or _nice(val)
    for c in extra:
        clean[c] = table[c].values
    if not clean["value"].notna().any():
        raise PrepError(f"'{val}' holds no numbers")
    if style == "choropleth" and not den:
        chk = cf.normalization_check(opt.get("value_name") or val, clean["value"].to_numpy(dtype="float64", na_value=float("nan")))
        if chk.get("raw_count_suspected") and not opt.get("rate_justification"):
            fs.append(finding("MAP-CART-001", "BLOCK",
                              f"'{val}' looks like a raw count: big places will look 'high' because they are big. Give denominator= (such as population) to map a rate, use style=proportional, "
                              f"or say why it is already a rate with rate_justification=. {'; '.join(chk.get('reasons') or [])}", chk.get("reasons")))
    cleaned = out / "market_table.csv"
    clean.to_csv(cleaned, index=False)
    states = opt.get("states")
    flt = {"column": "STUSPS", "in": list(states)} if states else None
    conf = "client-confidential" if _confidential(opt) else "public"
    if "confidential" not in opt:                # 0.24.0 (sample-map showcase): the default is said, so a public or sample table is not marked by mistake
        fs.append(finding("MAP-TEL-019", "INFO", "the table is treated as client-confidential (banner on, credited as a client table); for a public or "
                          "sample table pass confidential=false and credit= with its source"))
    n_rows = int(clean["value"].notna().sum())
    title = opt.get("title") or (f"{_nice(val)} by {geography}" if not den else f"{_nice(val)} per {per_s} {_per_word(den)}, by {geography}")
    message = opt.get("message") or (f"{val} varies across {n_rows} {geography[:-1] + 'ies' if geography.endswith('y') and n_rows != 1 else geography + ('s' if n_rows != 1 else '')}"
                                     + (f" in {', '.join(states)}" if states else "") + (f", shown per {per_s} {den}." if den else "."))
    spec = base_spec(opt.get("id") or _slug(title), message, title, opt.get("subtitle"), opt)
    join = {"table": cleaned.name, "left": left, "right": "key", "columns": [field_] + (["value"] if den and style == "proportional" else []) + extra,
            "pad": {"state": 2, "tract": 11}.get(geography, 5)}
    if left != "GEOID":
        join.pop("pad")
    credit = opt.get("credit") or f"Data: client table {Path(inp['table']).name}"
    src_ = {"type": "file", "path": str(bfile.resolve())} if bfile is not None else {"type": "ref", "id": ref}
    if bfile is not None and flt:
        flt = None
        fs.append(finding("MAP-TEL-019", "INFO", "states= is not applied to a boundary file (its state column is not known); the table's rows decide the areas"))
    base_layer = {"source": src_, "join": join, "confidentiality": conf, "provenance": {"attribution": credit}, **({"filter": flt} if flt else {})}
    if style == "choropleth":
        layer = {"id": "market", **base_layer, "role": "choropleth", "value_field": field_, "classification": {"method": opt.get("method") or "quantile", "classes": int(opt.get("classes") or 5)},
                 "legend": {"title": legend}, "palette": {"name": "brand_sequential"}}
        if opt.get("rate_justification") and not den:
            layer["override"] = {"MAP-CART-001": str(opt["rate_justification"])}
        spec["layers"].append(layer)
    else:
        ctx = {"id": "areas", "source": dict(src_), "role": "context", **({"filter": flt} if flt else {}), "legend": {"include": False}}
        spec["layers"].append(ctx)
        vf = "value" if den else field_
        spec["layers"].append({"id": "market", **base_layer, "role": "proportional", "centroids": True, "value_field": vf, "symbol": {"max_size": float(opt.get("max_size") or 14), "color": "@brand.accent"},
                               "legend": {"title": opt.get("legend") or _nice(val)}})
        spec["layers"][-1]["join"]["columns"] = [vf] + [c for c in extra]
    if opt.get("labels") and style == "proportional":
        spec["labels"] = [{"layer": "areas", "field": "NAME", "halo": True}]
    p = write_spec(spec, out)
    return {"spec": str(p), "findings": fs, "report": {"geography": geography, "key": key, "value": val, "denominator": den, "style": style, "rows": int(len(clean)),
                                                        "boundary": str(bfile.name) if bfile is not None else ref},
            "files": [str(cleaned)]}


# ---------------------------------------------------------------------------------------------------------------- program-map
def prepare_program(inp: dict, opt: dict, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    tr = tj.read_table(inp["tracker"], opt.get("sheet"))
    fs: list[dict] = []
    name_c = pick_column(tr.columns, opt.get("name"), ("name", "venue", "venue_name", "site", "site_name", "location", "project", "item", "title", "facility"), "name column")
    stat_c = pick_column(tr.columns, opt.get("status"), ("status", "rag", "rag_status", "state", "health", "overall_status", "progress", "phase_status"), "status column")
    if not name_c or not stat_c:
        raise PrepError(f"the tracker needs a name column and a status column (columns: {', '.join(map(str, tr.columns))}); say which with name= and status=")
    df = tr.copy()
    df["__name"] = df[name_c].map(lambda v: None if v is None or str(v).strip().lower() in ("", "nan") else str(v).strip())
    df = df[df["__name"].notna()].copy()
    cols = tg.detect_columns(tr.columns)
    lat_c, lon_c = opt.get("lat") or cols.get("lat"), opt.get("lon") or cols.get("lon")
    if inp.get("coordinates"):
        co = tj.read_table(inp["coordinates"], opt.get("coordinates_sheet"))
        cc = tg.detect_columns(co.columns)
        cn = pick_column(co.columns, opt.get("coordinates_name"), ("name", "venue", "venue_name", "site", "site_name", "location", "facility"), "name column of the coordinates table")
        if not cn or "lat" not in cc or "lon" not in cc:
            raise PrepError(f"the coordinates table needs a name, a latitude and a longitude column (found: {', '.join(map(str, co.columns))})")
        nk = lambda v: re.sub(r"[^a-z0-9]+", " ", str(v).lower()).strip()  # noqa: E731
        lut = {nk(r[cn]): (tg._num(r[cc["lat"]]), tg._num(r[cc["lon"]])) for _, r in co.iterrows()}
        df["__lat"] = df["__name"].map(lambda n: (lut.get(nk(n)) or (None, None))[0])
        df["__lon"] = df["__name"].map(lambda n: (lut.get(nk(n)) or (None, None))[1])
        src_note = f"{Path(inp['tracker']).name} (tracker) with positions from {Path(inp['coordinates']).name}"
    elif lat_c and lon_c:
        df["__lat"], df["__lon"] = df[lat_c].map(tg._num), df[lon_c].map(tg._num)
        src_note = f"{Path(inp['tracker']).name} (tracker with coordinates)"
    else:
        raise PrepError("the tracker has no latitude and longitude columns; add them or give a coordinates table (venue name, latitude, longitude). Nothing is looked up from addresses")
    nocoord = df["__lat"].isna() | df["__lon"].isna()
    if nocoord.any():
        fs.append(finding("MAP-TEL-040", "WARN", f"{int(nocoord.sum())} of {len(df)} row(s) have no coordinates and are not on the map: {', '.join(df.loc[nocoord, '__name'].astype(str).head(8))}"
                          + (" ..." if nocoord.sum() > 8 else ""), df.loc[nocoord, "__name"].astype(str).tolist()[:50]))
        df = df[~nocoord]
    if df.empty:
        raise PrepError("no row has both a name and coordinates")
    df["__status"] = df[stat_c].map(tg.normalize_status)
    unk = df["__status"].isna()
    if unk.any():
        words = sorted({str(v) for v in df.loc[unk, stat_c]})[:8]
        fs.append(finding("MAP-TEL-041", "WARN", f"{int(unk.sum())} row(s) have a status the plugin does not recognise ({', '.join(words)}); they are drawn as No data", words))
    labels = {k: lab for k, lab, _c, _s in tg.STATUSES}
    present = [k for k, *_ in tg.STATUSES if (df["__status"] == k).any()]
    clean = df[["__name", "__lat", "__lon"]].copy()
    clean.columns = ["name", "lat", "lon"]
    clean["status"] = df["__status"].map(lambda k: labels.get(k) if k else None)
    for extra in (opt.get("extra_columns") or []):
        if extra in df.columns:
            clean[extra] = df[extra].values
    cleaned = out / "program.csv"
    clean.to_csv(cleaned, index=False)
    spc = {k: (lab, c, s) for k, lab, c, s in tg.STATUSES}
    cls = {"method": "categorical", "order": [spc[k][0] for k in present], "colors": [spc[k][1] for k in present], "shapes": [spc[k][2] for k in present]}
    counts = {labels[k]: int((df["__status"] == k).sum()) for k in present}
    seed = opt.get("privacy_seed") if opt.get("privacy_seed") is not None else sp.default_seed("program")
    priv = _privacy(opt, int(seed))
    conf = "client-confidential" if opt.get("confidential") else "public"
    title = opt.get("title") or "Program status by location"
    worst = [labels[k] for k in ("delayed", "at-risk") if k in present]
    message = opt.get("message") or (f"{len(clean)} locations: " + ", ".join(f"{n} {lab.lower()}" for lab, n in counts.items()) + ".")
    bb = (float(clean["lon"].min()), float(clean["lat"].min()), float(clean["lon"].max()), float(clean["lat"].max()))
    spec = base_spec(opt.get("id") or _slug(title), message, title, opt.get("subtitle"), opt, bbox=bb)
    nb = pick_basemap(opt, bb)[1]
    if nb:
        fs.append(finding("MAP-TEL-050", "INFO", nb))
    spec["crs"] = {"display": "auto", "datum_note": DATUM_NOTE}
    layer = {"id": "program", "source": {"type": "file", "path": cleaned.name, "x": "lon", "y": "lat", "crs": "EPSG:4326"}, "role": "points", "value_field": "status",
             "classification": cls, "symbol": {"size": float(opt.get("size") or 9)}, "confidentiality": conf, "legend": {"title": opt.get("legend") or "Status"},
             "provenance": provenance(opt.get("source") or src_note, opt.get("license") or ("client-proprietary" if opt.get("confidential") else "public-domain"), opt.get("date"))}
    if priv:
        layer["privacy"] = priv
    spec["layers"].append(layer)
    if opt.get("labels", True) is not False and len(clean) <= int(opt.get("max_labels") or 40):
        spec["labels"] = [{"layer": "program", "field": "name", "halo": True}]
    p = write_spec(spec, out)
    summary = out / "program_summary.csv"
    clean.groupby("status", dropna=False).size().rename("locations").reset_index().to_csv(summary, index=False)
    return {"spec": str(p), "findings": fs, "report": {"locations": int(len(clean)), "counts": counts, "attention": worst, "name_column": name_c, "status_column": stat_c},
            "files": [str(cleaned), str(summary)], "extras": [str(summary)]}


PREPARE = {"coverage": prepare_coverage, "market": prepare_market, "program": prepare_program}


# ---------------------------------------------------------------------------------------------------------------- network-analysis (M11 B)
NA_ANALYSES = ("spacing", "best-server", "overlap", "backhaul", "neighbors", "drive-test", "sector-kpi", "population")
NA_ALIASES = {"best_server": "best-server", "bestserver": "best-server", "voronoi": "best-server", "cells": "best-server", "density": "spacing", "nearest": "spacing",
              "neighbours": "neighbors", "neighbour": "neighbors", "neighbor": "neighbors", "pci": "neighbors", "handover": "neighbors",
              "drive_test": "drive-test", "drivetest": "drive-test", "bins": "drive-test", "samples": "drive-test",
              "sector_kpi": "sector-kpi", "kpi": "sector-kpi", "households": "population", "pop": "population", "links": "backhaul", "fiber": "backhaul", "fibre": "backhaul"}
NA_NEEDS = {"backhaul": "links", "neighbors": "neighbors", "drive-test": "samples", "sector-kpi": "kpi", "population": "population"}
NA_TITLES = {"spacing": "Site spacing and density", "best-server": "Best-server cells by distance", "overlap": "Footprint overlap", "backhaul": "Backhaul topology",
             "neighbors": "Neighbour relations", "drive-test": "Drive-test bins", "sector-kpi": "Sector KPI", "population": "Population in footprints"}
EQUAL_UNITS = "each unit is the same size (an equal drawing radius, an H3 hexagon), so a count per unit compares fairly"


def parse_analyses(value) -> list[str]:
    names = value if isinstance(value, (list, tuple)) else str(value or "spacing").split(",")
    out = []
    for n in names:
        k = re.sub(r"\s+", "", str(n).strip().lower())
        k = NA_ALIASES.get(k, k)
        if k not in NA_ANALYSES:
            raise PrepError(f"'{n}' is not an analysis; choose from {', '.join(NA_ANALYSES)}")
        if k not in out:
            out.append(k)
    return out


def _read_any(path, sheet=None):
    """A table (csv, xlsx) as a DataFrame, or a geodata file as a GeoDataFrame."""
    p = Path(path)
    if not p.is_file():
        raise PrepError(f"'{p}' was not found")
    if p.suffix.lower() in tg.TABLE_SUFFIXES:
        return tj.read_table(p, sheet)
    import geo_io
    return geo_io.read(p).gdf


def _read_geo(path, what: str):
    import geo_io
    p = Path(path)
    if not p.is_file():
        raise PrepError(f"{what} '{p}' was not found")
    g = geo_io.read(p).gdf
    if g.crs is None:
        raise PrepError(f"{what} '{p.name}' states no CRS; convert it with a CRS first (geo-convert --crs-assume)")
    return g


def load_population(ref_or_path, opt: dict):
    """(zones gdf, provenance dict): a file, or a library entry id (ref.*) loaded as map-build loads it, with its vintage and attribution."""
    s = str(ref_or_path)
    if s.startswith("ref."):
        import map_plan as mp
        try:
            gdf, e = mp.load_ref_layer(s, opt.get("population_variant"))
        except mp.PlanError as ex:
            raise PrepError(str(ex), "MAP-REF-002") from None
        src = e.get("source") or {}
        prov = {"source": f"{src.get('publisher', '')}, {src.get('product', e.get('title', ''))}".strip(", "), "vintage": (e.get("vintage") or {}).get("current"),
                "license": (e.get("license") or {}).get("class"), "attribution": (e.get("license") or {}).get("attribution_text"), "library_ref": e["id"], "library_rev": e.get("rev")}
        return gdf, prov
    g = _read_geo(s, "population layer")
    prov = {"source": opt.get("population_source") or f"{Path(s).name} (supplied)", "vintage": opt.get("population_vintage"), "license": opt.get("population_license") or "unknown"}
    return g, {k: v for k, v in prov.items() if v}


def _uniq(gdf, field: str):
    import numpy as np
    x = gdf[field].to_numpy(dtype="float64", na_value=np.nan)
    return np.unique(x[np.isfinite(x)])


def _distinct(gdf, field: str) -> int:
    return int(_uniq(gdf, field).size)


def _breaks(gdf, field: str, classes: int) -> list[float]:
    """Class breaks that cannot collapse: midpoints between the distinct values when there are few, else midpoints at the quantiles of the
    distinct values (a count-heavy field such as sites per hexagon ties on the data quantiles and would fall below three classes)."""
    u = _uniq(gdf, field)
    k = int(max(3, min(classes, u.size)))
    if u.size <= k:
        return [float((a + b) / 2) for a, b in zip(u[:-1], u[1:])]
    idx = [int(round(i * (u.size - 1) / k)) for i in range(1, k)]
    return [float((u[i - 1] + u[i]) / 2) for i in sorted(set(idx)) if i >= 1]


def _value_layer(lid: str, path: str, gdf, field: str, legend: str, prov: dict, conf: str, *, diverging: bool = False, classes: int = 5, opacity: float = 0.75,
                 override: str | None = None, fmt: dict | None = None, kind: str = "polygon") -> dict:
    """A layer coloured by a numeric field: a choropleth (or classified lines) when there are enough distinct values, otherwise a categorical
    classification of the same field (a few counts) so the map never fails on too few classes."""
    import numpy as np
    role = "lines" if kind == "line" else "choropleth"
    u = _uniq(gdf, field)
    n = int(u.size)
    few_integers = n <= 4 and bool(n) and bool(np.all(u == np.round(u)))     # 1, 2, 3 sites: integer labels cannot split into three classes
    layer = {"id": lid, "source": {"type": "file", "path": path}, "role": role, "value_field": field, "confidentiality": conf, "provenance": prov, "legend": {"title": legend}}
    if kind != "line":
        layer["symbol"] = {"opacity": opacity, "stroke": "@brand.background", "stroke_width": 0.3}
    else:
        layer["symbol"] = {"width": 1.2}
    if n >= 3 and not few_integers:
        if diverging and n >= 8:
            cls = {"method": "quantile", "classes": int(min(classes, n)), "midpoint": "auto"}
        else:
            cls = {"method": "manual", "breaks": _breaks(gdf, field, classes)}
        if fmt:
            cls["format"] = fmt
        layer["classification"] = cls
        layer["palette"] = {"name": "brand_diverging" if diverging else "brand_sequential"}
        if override and role == "choropleth":
            layer["override"] = {"MAP-CART-001": override}
    elif n >= 1:
        layer["role"] = "polygons" if kind != "line" else "lines"
        order = [int(v) if float(v).is_integer() else float(v) for v in u][:8]
        layer["classification"] = {"method": "categorical", "max_categories": 8, "order": order}
        if few_integers and n >= 2:          # an ordered count keeps the sequential ramp: light for few, dark for many
            layer["classification"]["colors"] = [f"@brand.sequential.{int(round(i * 6 / (n - 1)))}" for i in range(n)]
        if kind != "line":
            layer["symbol"]["opacity"] = opacity
    else:
        layer.pop("value_field")
        layer["role"] = "polygons" if kind != "line" else "lines"
    return layer


def _sites_layer(path: str, prov: dict, conf: str, priv: dict | None, *, size: float = 2.4, title: str = "Site") -> dict:
    layer = {"id": "sites", "source": {"type": "file", "path": path}, "role": "points", "confidentiality": conf, "provenance": prov,
             "symbol": {"shape": "circle", "size": size, "color": "@brand.emphasis"}, "legend": {"title": title, "label": title}}
    if priv:
        layer["privacy"] = priv
    return layer


def _table_csv(gdf, path: Path) -> Path:
    """The attributes of a layer as CSV with no geometry and no coordinate columns (safe for a confidential site list)."""
    import pandas as pd
    df = pd.DataFrame(gdf.drop(columns=[gdf.geometry.name])) if hasattr(gdf, "geometry") else pd.DataFrame(gdf)
    drop = [c for c in df.columns if _norm(c) in ("lat", "lon", "latitude", "longitude", "x", "y", "lng", "easting", "northing")]
    df = df.drop(columns=drop)
    df.to_csv(path, index=False)
    return path


def prepare_network(inp: dict, opt: dict, out: Path) -> dict:
    """Run the analyses named in `analysis=` on the site list and write, per analysis, its layer files (GPKG), its attribute tables (CSV) and a
    MapSpec under out/<analysis>/. Returns {"specs": {name: path}, "layers": {name: {layer: path}}, "tables": [...], "notes": {name: [...]},
    "summary": {name: {...}}, "findings": [...], "report": {...}}."""
    import geo_io
    import telecom_analytics as ta
    out.mkdir(parents=True, exist_ok=True)
    opt = dict(opt)
    opt.setdefault("audience", "internal")
    analyses = parse_analyses(opt.get("analysis"))
    for a in analyses:
        need = NA_NEEDS.get(a)
        if need and not opt.get(need):
            raise PrepError(f"analysis '{a}' needs {need}=FILE" + (" (a file, or a library entry id)" if need == "population" else ""))
    df, fs, rep = tg.ingest_sites(inp["sites"], opt.get("sheet"), columns=opt.get("columns"), layer=opt.get("layer"))
    if opt.get("band"):
        if "band" not in df.columns:
            raise PrepError("band= was given but the site list has no band column")
        want = str(opt["band"]).strip().lower()
        keep = df["band"].astype(str).str.strip().str.lower() == want
        if not keep.any():
            raise PrepError(f"no row of the site list has band {opt['band']} (bands: {', '.join(rep['bands'])})")
        fs.append(finding("MAP-TEL-019", "INFO", f"{int(keep.sum())} of {len(df)} row(s) are band {opt['band']}; the rest were left out"))
        df = df[keep].reset_index(drop=True)
    sites = ta.site_points(df)
    conf = "client-confidential" if _confidential(opt) else "public"
    seed = opt.get("privacy_seed") if opt.get("privacy_seed") is not None else sp.default_seed("sites")
    priv = _privacy(opt, int(seed))
    src_name = Path(inp["sites"]).name
    lic = opt.get("license") or ("client-proprietary" if _confidential(opt) else "public-domain")
    site_prov = provenance(opt.get("source") or f"{src_name} (site list supplied by the client)", lic, opt.get("date"))
    wedges = None
    if any(a in ("overlap", "sector-kpi", "population") for a in analyses):
        rf = opt.get("radius_field") or ("radius_m" if "radius_m" in df.columns else None)       # 0.25.0 (TODO M6): a radius per sector
        wedges, wf = tg.make_wedges(df, radius_m=float(opt.get("radius_m") or tg.DEFAULT_RADIUS_M), beamwidth=opt.get("beamwidth"), radius_field=rf)
        fs += wf
        if "site_id" in wedges.columns:
            if "sector" not in wedges.columns:
                wedges["sector"] = wedges["site_id"].astype(str) + "-" + (wedges.groupby("site_id").cumcount() + 1).astype(str)
            wedges = wedges.rename(columns={"site_id": "site"})      # as coverage-map does: a site id repeats per sector and is not a key
    if priv and any(a in ("best-server", "overlap", "backhaul", "neighbors", "sector-kpi", "population", "spacing") for a in analyses):
        fs.append(finding("MAP-TEL-063", "INFO", "the coordinate reduction applies to the site symbols; cells, hexagons, lines and footprints are measured and drawn from the true positions, "
                          "so keep a map that shows them internal or accept that a reader can place the sites"))
    res: dict = {"specs": {}, "layers": {}, "tables": [], "notes": {}, "summary": {}, "findings": fs, "files": []}
    bbox = (float(df["lon"].min()), float(df["lat"].min()), float(df["lon"].max()), float(df["lat"].max()))
    nb = pick_basemap(opt, bbox)[1]
    if nb:
        fs.append(finding("MAP-TEL-050", "INFO", nb))
    base_id = opt.get("id") or _slug(opt.get("title") or Path(inp["sites"]).stem or "network")

    def derived(what: str, a: str) -> dict:
        return provenance(f"{what}, derived from {src_name} by network-analysis ({a})", lic, opt.get("date"))

    def write_layers(a: str, layers: dict, adir: Path) -> dict:
        paths = {}
        for name, g in layers.items():
            p = adir / f"{name}.gpkg"
            geo_io.write(g.to_crs("EPSG:4326") if g.crs is not None and not g.crs.equals("EPSG:4326") else g, p, "gpkg", layer=name, overwrite=True)
            paths[name] = p.name
            res["files"].append(str(p))
            res["layers"].setdefault(a, {})[name] = str(p)
            t = _table_csv(g, adir / f"{a}_{name}.csv")
            res["tables"].append(str(t))
        return paths

    for a in analyses:
        adir = out / a
        adir.mkdir(parents=True, exist_ok=True)
        try:
            r, spec = _run_analysis(a, inp, opt, df, sites, wedges, conf, priv, site_prov, derived, adir, write_layers, base_id, bbox)
        except (ta.AnalyticsError, tj.JoinError, PrepError) as ex:
            raise PrepError(f"{a}: {ex}", getattr(ex, "fid", "MAP-RUN-002")) from None
        except (geo_io.GeoIOError, ValueError) as ex:
            raise PrepError(f"{a}: {ex}", "MAP-RUN-002") from None
        spec["crs"] = {"display": "auto", "datum_note": DATUM_NOTE}
        p = write_spec(spec, adir)
        res["specs"][a] = str(p)
        res["notes"][a] = list(r.notes) + ([tg.WEDGE_NOTE] if a in ("overlap", "sector-kpi", "population") else [])
        res["summary"][a] = r.summary
        fs += [dict(f, analysis=a) for f in r.findings]
    res["report"] = {**rep, "analyses": analyses, "base_id": base_id, "sites": int(len(sites)), "sectors": int(len(df)), "band": opt.get("band"), "audience": opt["audience"],
                     "summary": {a: _plain(res["summary"][a]) for a in analyses}}
    return res


def _plain(o):
    """JSON-safe copy of a summary (numpy scalars and NaN become plain values)."""
    import math as _m
    if isinstance(o, dict):
        return {str(k): _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    if hasattr(o, "item"):
        o = o.item()
    if isinstance(o, float) and not _m.isfinite(o):
        return None
    return o


def _run_analysis(a: str, inp: dict, opt: dict, df, sites, wedges, conf: str, priv, site_prov: dict, derived, adir: Path, write_layers, base_id: str, bbox):
    import telecom_analytics as ta
    sheet = opt.get("sheet")
    sid = f"{base_id}-{a}"
    sub = opt.get("subtitle")
    if a == "spacing":
        r = ta.site_spacing(sites, resolution=opt.get("density_resolution", "auto"))
        # the layer file calls it nearest_site: the data lint reads a column ending in _id as a key and would warn on the repeats
        paths = write_layers(a, {"sites": r.layers["sites"].rename(columns={"nearest_site_id": "nearest_site"}), "density": r.layers["density"]}, adir)
        st, dens = r.summary["nearest"], r.summary["sites_per_km2"]
        spec = base_spec(sid, opt.get("message") or f"Sites are a median {_n2(st['median'])} km apart; the densest hexagon holds {_n2(dens['max'])} sites per km².",
                         opt.get("title") or NA_TITLES[a], sub, opt, bbox=bbox)
        cell_km2 = float(r.layers["density"]["cell_km2"].median())
        spec["layers"] = [_value_layer("density", paths["density"], r.layers["density"], "count", f"Sites per hexagon (about {_n2(cell_km2)} km²)", derived("H3 hexagons with the sites counted in each", a), conf, opacity=0.7, override=EQUAL_UNITS),
                          _sites_layer(paths["sites"], site_prov, conf, priv)]
    elif a == "best-server":
        clip = _read_geo(opt["clip"], "clip polygon") if opt.get("clip") else None
        r = ta.best_server(sites, clip=clip, extend_km=float(opt["extend_km"]) if opt.get("extend_km") else None)
        paths = write_layers(a, {"cells": r.layers["cells"], "sites": sites}, adir)
        st = r.summary["area_km2"]
        spec = base_spec(sid, opt.get("message") or f"{r.summary['cells']} best-server cells by distance: a median {_n2(st['median'])} km² each, from {_n2(st['min'])} to {_n2(st['max'])} km².",
                         opt.get("title") or NA_TITLES[a], sub or "Distance only: no propagation model", opt, bbox=bbox)
        spec["layers"] = [_value_layer("cells", paths["cells"], r.layers["cells"], "area_km2", "Best-server cell area (km²)", derived("Voronoi cells, one per site", a), conf, opacity=0.6,
                                       override="the value is each cell's own area, which is what the map shows"),
                          _sites_layer(paths["sites"], site_prov, conf, priv)]
    elif a == "overlap":
        by = opt.get("by") or ("band" if "band" in wedges.columns else None)
        r = ta.footprint_overlap(wedges, by=by)
        paths = write_layers(a, {"footprints": r.layers["footprints"], "sites": sites}, adir)
        row = r.summary["table"][0]
        spec = base_spec(sid, opt.get("message") or f"{_n2(100 * row['share_2plus'])} percent of the drawn footprint area lies under two or more footprints; {row['with_overlap']} of {row['footprints']} sectors overlap another.",
                         opt.get("title") or NA_TITLES[a], sub or "Drawn footprints, not predicted coverage", opt, bbox=bbox)
        present = [c for c in ta.OVERLAP_CLASSES if (r.gdf["overlap_class"] == c).any()]
        spec["layers"] = [{"id": "footprints", "source": {"type": "file", "path": paths["footprints"]}, "role": "polygons", "value_field": "overlap_class",
                           "classification": {"method": "categorical", "order": present, "max_categories": 8,
                                              "colors": [c for k, c in zip(ta.OVERLAP_CLASSES, ("@brand.sequential.0", "@brand.status.warning", "@brand.status.danger")) if k in present]},
                           "confidentiality": conf, "provenance": derived("sector wedges (display geometry)", a), "symbol": {"opacity": 0.8, "stroke": "@brand.frame", "stroke_width": 0.4},
                           "legend": {"title": "Overlapping footprints"}},
                          _sites_layer(paths["sites"], site_prov, conf, priv)]
    elif a == "backhaul":
        links = _read_any(opt["links"], sheet)
        r = ta.backhaul_topology(sites, links)
        paths = write_layers(a, {"links": r.layers["links"], "sites": r.layers["sites"]}, adir)
        hub = r.summary["hubs"][0] if r.summary["hubs"] else None
        spec = base_spec(sid, opt.get("message") or (f"{r.summary['links']} backhaul links total {_n2(r.summary['total_km'])} km; {hub['site']} carries the most with {hub['degree']}." if hub else f"{r.summary['links']} backhaul links."),
                         opt.get("title") or NA_TITLES[a], sub or "Straight lines between sites, not the built route", opt, bbox=bbox)
        tcol = next((c for c in r.gdf.columns if _norm(c) in ("type", "link_type", "medium", "technology")), None)
        link = {"id": "links", "source": {"type": "file", "path": paths["links"]}, "role": "lines", "confidentiality": conf, "provenance": derived("straight lines between linked sites", a),
                "symbol": {"width": 1.4, "color": "@brand.accent"}, "legend": {"title": "Backhaul link"}}
        if tcol and r.gdf[tcol].notna().any():
            link["value_field"], link["classification"] = tcol, {"method": "categorical", "max_categories": 8}
            link["legend"] = {"title": "Backhaul link by " + _nice(tcol).lower()}
        spec["layers"] = [link, {"id": "sites", "source": {"type": "file", "path": paths["sites"]}, "role": "proportional", "value_field": "degree", "confidentiality": conf, "provenance": site_prov,
                                 "symbol": {"max_size": 7, "color": "@brand.emphasis"}, "legend": {"title": "Links at the site", "format": {"decimals": 0}}, **({"privacy": priv} if priv else {})}]
    elif a == "neighbors":
        nb = _read_any(opt["neighbors"], sheet)
        cells = _read_any(opt["cells"], sheet) if opt.get("cells") else None
        r = ta.neighbor_relations(sites, nb, cells=cells)
        paths = write_layers(a, {"relations": r.layers["relations"], "sites": sites}, adir)
        n = r.summary["relations"]
        if r.summary.get("success_rate"):
            msg = f"{n} neighbour relations; {r.summary['below_90']} have a handover success rate below 90 percent."
            rel = _value_layer("relations", paths["relations"], r.gdf, "success_pct", "Handover success (%)", derived("lines between the sites of neighbour-cell pairs", a), conf,
                               diverging=True, classes=4, fmt={"suffix": "%"}, kind="line")
        else:
            msg = f"{n} neighbour relations between {r.gdf[['source_site', 'target_site']].stack().nunique()} sites."
            rel = {"id": "relations", "source": {"type": "file", "path": paths["relations"]}, "role": "lines", "confidentiality": conf, "provenance": derived("lines between the sites of neighbour-cell pairs", a),
                   "symbol": {"width": 0.9, "color": "@brand.accent"}, "legend": {"title": "Neighbour relation"}}
        spec = base_spec(sid, opt.get("message") or msg, opt.get("title") or NA_TITLES[a], sub, opt, bbox=bbox)
        spec["layers"] = [rel, _sites_layer(paths["sites"], site_prov, conf, priv)]
    elif a == "drive-test":
        pts = _read_geo(opt["samples"], "drive-test samples")
        agg = str(opt.get("agg") or "mean")
        r = ta.drive_test_bins(pts, opt.get("kpi_field") or opt.get("value"), resolution=opt.get("resolution", "auto"), agg=agg, min_count=int(opt.get("min_count") or 1))
        paths = write_layers(a, {"bins": r.layers["bins"], "sites": sites}, adir)
        vf = r.summary["value_field"] or "count"
        st = r.summary["kpi_stats"] or r.summary["count_stats"]
        label = (_nice(r.summary["kpi"]) + f" ({agg})") if r.summary["kpi"] else "Samples per hexagon"
        spec = base_spec(sid, opt.get("message") or f"{label} runs from {_n2(st['min'])} to {_n2(st['max'])} across {r.summary['cells']} hexagons with at least {r.summary['min_count']} samples.",
                         opt.get("title") or (f"{_nice(r.summary['kpi'])} by hexagon" if r.summary["kpi"] else NA_TITLES[a]), sub or "Measured samples, binned; empty cells were not measured", opt, bbox=bbox)
        spec["layers"] = [_value_layer("bins", paths["bins"], r.gdf, vf, label, provenance(opt.get("samples_source") or f"{Path(opt['samples']).name} (drive-test or crowd samples, supplied)", opt.get("samples_license") or lic_of(opt), opt.get("date")),
                                       conf, opacity=0.8, override=EQUAL_UNITS, diverging=bool(opt.get("diverging"))),
                          _sites_layer(paths["sites"], site_prov, conf, priv)]
    elif a == "sector-kpi":
        kpi = _read_any(opt["kpi"], sheet)
        key = opt.get("key") or "sector"
        key = [k.strip() for k in key.split("+")] if isinstance(key, str) and "+" in key else key
        r = ta.sector_kpi(wedges, kpi, key=key, value=opt.get("value"), diverging=opt.get("diverging"))
        paths = write_layers(a, {"sectors": r.layers["sectors"], "sites": sites}, adir)
        v, st = r.summary["value"], r.summary["stats"]
        spec = base_spec(sid, opt.get("message") or f"{_nice(v)} runs from {_n2(st['min'])} to {_n2(st['max'])} across {r.summary['matched']} sectors (median {_n2(st['median'])}).",
                         opt.get("title") or f"{_nice(v)} by sector", sub or "Sector wedges are drawn, not predicted coverage", opt, bbox=bbox)
        spec["layers"] = [_value_layer("sectors", paths["sectors"], r.gdf, v, opt.get("legend") or _nice(v), derived(f"sector wedges (display geometry) joined to {Path(opt['kpi']).name}", a), conf,
                                       diverging=r.summary["diverging"], opacity=0.85, override=EQUAL_UNITS),
                          _sites_layer(paths["sites"], site_prov, conf, priv)]
    elif a == "population":
        zones, zprov = load_population(opt["population"], opt)
        radius = opt.get("radius") or (f"{opt['radius_km']} km" if opt.get("radius_km") else None)
        r = ta.population_in_footprints(wedges, zones, value=opt.get("population_value"), sites=sites if radius else None, radius=radius, zone_provenance=zprov)
        paths = write_layers(a, {k: v for k, v in r.layers.items() if k != "sites"} | {"sites": sites}, adir)
        val = r.summary["value"]
        vint = f" ({zprov['source']}, {zprov['vintage']})" if zprov.get("vintage") and zprov.get("source") else (f" ({zprov['vintage']})" if zprov.get("vintage") else "")
        zone_prov = provenance(zprov.get("source") or f"{opt['population']} (supplied)", zprov.get("license") or "unknown", opt.get("date"), **({"vintage": zprov["vintage"]} if zprov.get("vintage") else {}),
                               **({"attribution": zprov["attribution"]} if zprov.get("attribution") else {}), **({"library_ref": zprov["library_ref"]} if zprov.get("library_ref") else {}))
        if "rings" in r.layers:
            ws = r.summary["within_radius"]
            fld = next(c for c in r.layers["rings"].columns if c.startswith(f"{val}_within_"))
            spec = base_spec(sid, opt.get("message") or f"{_nice(val)} within {r.summary['radius']} of each site: a median {_n2(ws['median'])}, from {_n2(ws['min'])} to {_n2(ws['max'])}{vint}.",
                             opt.get("title") or f"{_nice(val)} within {r.summary['radius']} of each site", sub or "A distance band, not a service area", opt, bbox=bbox)
            spec["layers"] = [_value_layer("rings", paths["rings"], r.layers["rings"], fld, f"{_nice(val)} within {r.summary['radius']}", dict(zone_prov, source=f"{zone_prov['source']}, apportioned into circles of {r.summary['radius']} around each site"),
                                           conf, opacity=0.55, override=EQUAL_UNITS), _sites_layer(paths["sites"], site_prov, conf, priv)]
        else:
            spec = base_spec(sid, opt.get("message") or f"The drawn sector footprints hold {_n2(r.summary['covered_total'])} of the {_n2(r.summary['zone_total'])} {val} in the area{vint}.",
                             opt.get("title") or f"{_nice(val)} under each sector footprint", sub or "Drawn footprints, not predicted coverage", opt, bbox=bbox)
            spec["layers"] = [_value_layer("footprints", paths["footprints"], r.layers["footprints"], f"{val}_covered", f"{_nice(val)} under the footprint", dict(zone_prov, source=f"{zone_prov['source']}, apportioned into the sector wedges (display geometry)"),
                                           conf, opacity=0.8, override=EQUAL_UNITS), _sites_layer(paths["sites"], site_prov, conf, priv)]
    else:
        raise PrepError(f"unknown analysis '{a}'")
    return r, spec


def lic_of(opt: dict) -> str:
    return opt.get("license") or ("client-proprietary" if _confidential(opt) else "public-domain")


def _n2(x) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return str(x)
    if x != x:
        return "n/a"
    return f"{x:,.0f}" if abs(x) >= 100 or float(x).is_integer() else f"{x:,.2f}".rstrip("0").rstrip(".")


PREPARE["network"] = prepare_network


def main(argv=None) -> int:
    from pipelines import kv
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("kind", choices=list(PREPARE))
    ap.add_argument("--sites")
    ap.add_argument("--table")
    ap.add_argument("--tracker")
    ap.add_argument("--coordinates")
    ap.add_argument("--out", required=True)
    ap.add_argument("--option", action="append", default=[])
    a = ap.parse_args(argv)
    inp = {k: v for k, v in {"sites": a.sites, "table": a.table, "tracker": a.tracker, "coordinates": a.coordinates}.items() if v}
    try:
        res = PREPARE[a.kind](inp, kv(a.option), Path(a.out))
    except (PrepError, ValueError, tj.JoinError) as ex:
        print(json.dumps({"ok": False, "findings": [finding(getattr(ex, "fid", "MAP-RUN-002"), "BLOCK", str(ex))]}, indent=2))
        return 3
    print(json.dumps({"ok": not any(f["severity"] == "BLOCK" for f in res["findings"]), **res}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
