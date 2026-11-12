#!/usr/bin/env python3
"""ref_fetch: bring a `fetch`-mode library entry into the local cache so a map can use it (spec 9.3.2 delivery modes).

    ref_fetch.py fetch ENTRY_ID [--variant 04] [--refresh]    download, verify the hash when the entry states one, convert to GeoParquet
    ref_fetch.py status ENTRY_ID [--variant 04]               where the cached copy is, or that there is none

Only the URL written in the entry is ever fetched, only over HTTPS, and only for an entry that is `active` (draft, blocked and
retired entries are refused). The cache is `.geo-mapping/cache/library/`; it is never part of a build artifact. No keys are sent.

The download is read as what it is: a zipped shapefile (or a zip holding several, a GeoJSON, a GeoPackage or a CSV), a GeoJSON or
TopoJSON, a GeoPackage, a CSV, a FlatGeobuf, a KML/KMZ or a Parquet file. The first bytes decide; the entry's `delivery.format`, the
URL's extension and the response's Content-Type are checked against them and a disagreement is reported, never acted on quietly.

M13: a gzip file (`.csv.gz`, `.geojson.gz`) is decompressed (at most MAX_DECOMPRESSED) and read as what it holds. A file whose layout the
entry declares is read by that declaration, not by guessing: `delivery.header: false` with `delivery.columns` (the names, in order) and
`delivery.delimiter` (`"\\t"`, `","`, `"|"`) for header-less text such as the GeoNames dump, alone or inside a zip (`delivery.member` names the
file when the zip holds several); `delivery.geometry_columns` (`{lon: X, lat: Y}`, `{wkt: COL}` or `{wkb: COL}`, CRS `delivery.geometry_crs`,
default EPSG:4326) for a CSV with several coordinate pairs or a plain Parquet without GeoParquet metadata. A row without coordinates keeps
its attributes and has no geometry. `delivery.rolling: true` says the publisher rewrites the file in place (GeoNames, daily): the recorded
hash and count are those verified on `url_verified_on`, and a fetch that differs is used, with the difference reported (the count within
`delivery.features_tolerance_pct`, default 5).

    ref_fetch.py read ENTRY_ID FILE [--out OUT] [--bbox=W,S,E,N]   read a file you supplied by the entry's declared layout (Ookla's WKT
                                                                   tiles: `delivery.quadkey_column` lets a box skip the rest of the world)

0.6.0 (credentials spec §7.2, §10): an entry that needs a key says so in `delivery.requires_credential` (name, kind, host, how it is sent, how
the service says no). Its request text carries no key and no variable. The fetch resolves `cred:NAME` from the person's own credential folder
in this process (host-side in the MCP server's `ref_fetch` tool; in-process with the folder connected as the fallback), adds the key at send
time, follows no redirect, and stops on the first rejection (MAP-CRED-008). Without a registered credential it stops with "run map-credentials
add NAME" (MAP-CRED-013). A `service`-mode entry with a REST JSON request (the Census Data API) is fetched once and cached as a table:

    ref_fetch.py plan  ENTRY_ID [--variant 04] [--level tract]     the run card: the request with key=***, the credential name and host
    ref_fetch.py fetch ENTRY_ID [--variant 04] [--level tract]     the table, cached as Parquet with a GEOID column, and a record beside it
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gmap_common as gc  # noqa: E402

MAX_BYTES = 400 * 1024 * 1024
MAX_DECOMPRESSED = 1024 * 1024 * 1024     # a gzip or zip member is read up to 1 GB uncompressed (decompression-bomb guard)
UA = "geo-mapping-plugin/0.1 (reference-library fetch)"
VARIANT = re.compile(r"^[0-9a-z]{1,8}$")
DELIMITERS = {"\\t": "\t", "tab": "\t", "\t": "\t", ",": ",", "comma": ",", "|": "|", "pipe": "|", ";": ";", "semicolon": ";"}
TEXT_MEMBERS = (".txt", ".csv", ".tsv", ".tab", ".dat")


class FetchError(Exception):
    def __init__(self, message: str, code: int = gc.EXIT_RUNTIME, rule: str | None = None):
        super().__init__(message)
        self.code = code
        self.rule = rule


def cache_dir() -> Path:
    """The library cache: in the folder a tool call named (MCP work_folder=), else the working folder (0.9.4)."""
    return gc.outputs_dir() / "cache" / "library"


def cache_name(entry_id: str, variant: str | None = None) -> str:
    return f"{entry_id}{'__' + variant if variant else ''}.parquet"


def cached_path(entry: dict, variant: str | None = None) -> Path | None:
    p = cache_dir() / cache_name(entry["id"], variant)
    return p if p.is_file() else None


def source_url(entry: dict, variant: str | None) -> tuple[str, bool]:
    """(url, hash_applies). The `source.file_sha256` describes the entry's main URL only, never a per-variant URL."""
    d = entry.get("delivery") or {}
    if variant:
        t = d.get("url_template")
        if not t:
            raise FetchError(f"{entry['id']} has no per-variant file; fetch it without --variant", gc.EXIT_USAGE)
        if not VARIANT.match(variant):
            raise FetchError(f"variant '{variant}' is not a plain code such as a state FIPS (04)", gc.EXIT_USAGE)
        return t.format(statefp=variant, st=variant, variant=variant), False
    url = (entry.get("source") or {}).get("url")
    if not url:
        raise FetchError(f"{entry['id']} records no download URL", gc.EXIT_REFUSED)
    return url, True


def download_with_type(url: str, dest: Path, opener=None) -> tuple[str, str | None]:
    """Stream a URL to dest. Returns (sha256, content-type or None). Only HTTPS, only up to MAX_BYTES."""
    u = urllib.parse.urlparse(url)
    if u.scheme != "https":
        raise FetchError("only HTTPS downloads are allowed", gc.EXIT_REFUSED)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    h, total, ctype = hashlib.sha256(), 0, None
    open_ = opener or urllib.request.urlopen
    try:
        with open_(req, timeout=120) as r, open(dest, "wb") as f:
            headers = getattr(r, "headers", None)
            if headers is not None:
                try:
                    ctype = headers.get("Content-Type")
                except Exception:   # noqa: BLE001 - a header object without get() just gives no content type
                    ctype = None
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    raise FetchError(f"the download is larger than {MAX_BYTES // (1024 * 1024)} MB; stopped", gc.EXIT_REFUSED)
                h.update(chunk)
                f.write(chunk)
    except FetchError:
        raise
    except Exception as ex:   # network, TLS, HTTP errors
        raise FetchError(f"could not download {u.netloc}: {ex}", gc.EXIT_SERVICE)
    return h.hexdigest(), (str(ctype) if ctype else None)


def download(url: str, dest: Path, opener=None) -> str:
    """Stream a URL to dest and return its sha256 (ref_maint's verify uses this form)."""
    return download_with_type(url, dest, opener)[0]


def download_safe(url: str, dest: Path, http, auth=None) -> tuple[str, str | None]:
    """The same through SafeHTTP (public addresses only, GET, capped bytes and time, audited): the host-side MCP tool's route, and the only route
    for an entry that needs a credential (added at send time, never followed across a redirect)."""
    import ogc_http as oh
    if urllib.parse.urlparse(url).scheme != "https":
        raise FetchError("only HTTPS downloads are allowed", gc.EXIT_REFUSED)
    try:
        r = http.get(url, expect="any", auth=auth, max_bytes=MAX_BYTES)
    except oh.Refusal as ex:
        raise FetchError(str(ex), gc.EXIT_REFUSED, ex.rule)
    except oh.ServiceError as ex:
        raise FetchError(str(ex), gc.EXIT_SERVICE, ex.rule)
    dest.write_bytes(r.content)
    return hashlib.sha256(r.content).hexdigest(), r.content_type or None


# ---------------------------------------------------------------------------------------------------------------- 0.6.0: entries that need a credential
def credential_for(entry: dict, url: str, store=None):
    """The resolved credential an entry's request needs, or None when it needs none. FetchError MAP-CRED-013 when none is registered, or when the
    registered host is not the request's host."""
    import credentials as cr
    b = cr.entry_block(entry)
    if not b:
        return None
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if (b.get("host") or "").lower() != host:
        raise FetchError(f"{entry['id']}: the credential '{b.get('name')}' is declared for {b.get('host')}, but the request goes to {host}", gc.EXIT_REFUSED, "MAP-CRED-013")
    try:
        return cr.resolve(f"cred:{b['name']}", store=store, host=host, block=b)
    except cr.CredentialError as ex:
        raise FetchError(ex.findings[0]["message"], gc.EXIT_LINT if ex.rule in ("MAP-CRED-002", "MAP-CRED-003") else gc.EXIT_REFUSED, ex.rule)


def request_url(entry: dict, variant: str | None = None, level: str | None = None) -> str:
    """The REST request of a service-mode entry, for one level and state. The entry's text carries no key and no variable (MAP-CRED-001)."""
    d = entry.get("delivery") or {}
    reqs = d.get("requests") or {}
    if level:
        tpl = reqs.get(level)
        if not tpl:
            raise FetchError(f"{entry['id']} has no request for level '{level}'; the levels are {', '.join(reqs) or 'none'}", gc.EXIT_USAGE)
    else:
        tpl = d.get("request")
    if not tpl:
        raise FetchError(f"{entry['id']} records no request", gc.EXIT_REFUSED)
    if re.search(r"(?i)[?&](key|api_key|apikey|token|access_token)=", tpl) or "$" in tpl:
        raise FetchError(f"{entry['id']}: the request text carries a key or a variable; a library entry names the credential in requires_credential instead", gc.EXIT_LINT, "MAP-CRED-001")
    if any(t in tpl for t in ("{statefp}", "{st}", "{variant}")):
        if not variant:
            raise FetchError(f"{entry['id']} needs --variant: a state FIPS code such as 04", gc.EXIT_USAGE)
        if not VARIANT.match(variant):
            raise FetchError(f"variant '{variant}' is not a plain code such as a state FIPS (04)", gc.EXIT_USAGE)
        tpl = tpl.replace("{statefp}", variant).replace("{st}", variant).replace("{variant}", variant)
    elif variant:
        raise FetchError(f"{entry['id']} takes no --variant at this level", gc.EXIT_USAGE)
    if not tpl.startswith("https://"):
        raise FetchError("only HTTPS requests are sent", gc.EXIT_REFUSED)
    return tpl


def run_card(entry: dict, variant: str | None = None, level: str | None = None) -> dict:
    """What will be sent, shown before it is: the request with key=***, the credential's name and host. Never a value or a file path."""
    import credentials as cr
    url = request_url(entry, variant, level)
    b = cr.entry_block(entry)
    shown = url
    if b:
        inj = cr.parse_inject(b.get("inject")) or {}
        q = cr.inject_query_shown(inj)
        shown = cr.shown_url(url, inj) + (("&" if "?" in url else "?") + q if q else "")
    return {"entry": entry["id"], "rev": entry.get("rev"), "request": shown, "method": "GET, once; redirects are not followed",
            "credential": f"cred:{b['name']}" if b else None, "host": (b or {}).get("host") or urllib.parse.urlsplit(url).hostname,
            "sent_with": (cr._inject_shown(cr.parse_inject(b.get("inject"))) if b else None),
            "where": "the key is read and added on your computer at send time; it is never shown, logged or written" if b else None}


CENSUS_GEO = ("state", "county", "tract", "block group")
CENSUS_VAR = re.compile(r"^[A-Z]\d{5}[A-Z]?_\d{3}[A-Z]{1,2}$")
CENSUS_SENTINELS = {-999999999, -888888888, -666666666, -555555555, -333333333, -222222222}


def fetch_service(entry: dict, variant: str | None = None, level: str | None = None, refresh: bool = False, http=None, store=None) -> dict:
    """A service-mode REST JSON entry (the Census Data API): one GET, the answer cached as a Parquet table with a GEOID column, and a record beside
    it that names the credential and host, never the value or the file."""
    import ogc_http as oh
    if entry.get("status") != "active":
        raise FetchError(f"{entry['id']} is {entry.get('status')}; only active entries are fetched", gc.EXIT_REFUSED)
    d = entry.get("delivery") or {}
    if d.get("mode") != "service" or d.get("protocol") != "rest-json":
        raise FetchError(f"{entry['id']} is not a REST JSON service entry", gc.EXIT_USAGE)
    card = run_card(entry, variant, level)
    url = request_url(entry, variant, level)
    tag = "-".join(x for x in (level, variant) if x) or None
    out = cache_dir() / cache_name(entry["id"], tag)
    rec_path = out.with_suffix(".json")
    if out.is_file() and not refresh:
        return {"id": entry["id"], "rev": entry.get("rev"), "variant": variant, "level": level, "path": str(out), "record": str(rec_path), "cached": True, "run_card": card}
    auth = credential_for(entry, url, store)
    http = http or oh.SafeHTTP(limits={"timeout_s": 60, "max_bytes": 100_000_000, "retries": 1})
    try:
        r = http.get(url, expect="json", auth=auth)
    except oh.Refusal as ex:
        raise FetchError(str(ex), gc.EXIT_REFUSED, ex.rule)
    except oh.ServiceError as ex:
        raise FetchError(str(ex), gc.EXIT_SERVICE, ex.rule)
    finally:
        if auth is not None:
            auth.close()
    try:
        rows = json.loads(r.content)
        assert isinstance(rows, list) and rows and all(isinstance(x, list) for x in rows)
    except (ValueError, AssertionError):
        raise FetchError(f"{entry['id']}: the service did not answer with a table (a JSON list of rows)", gc.EXIT_SERVICE, "MAP-SVC-013")
    import pandas as pd
    df = pd.DataFrame(rows[1:], columns=[str(c) for c in rows[0]])
    notes = []
    geo = [c for c in CENSUS_GEO if c in df.columns]
    if geo:
        df["GEOID"] = df[geo].astype(str).agg("".join, axis=1)
        notes.append("GEOID = " + " + ".join(geo) + " (leading zeros kept)")
    blanked = 0
    for c in [c for c in df.columns if CENSUS_VAR.match(c)]:
        v = pd.to_numeric(df[c], errors="coerce")
        bad = v.isin(list(CENSUS_SENTINELS))
        blanked += int(bad.sum())
        df[c] = v.mask(bad)
    if blanked:
        notes.append(f"{blanked} value(s) were Census 'not available' codes and are left empty")
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    import datetime as _dt
    record = {"fetch_version": 1, "id": entry["id"], "rev": entry.get("rev"), "variant": variant, "level": level,
              "retrieved_at": __import__("os").environ.get("GMAP_FIXED_DATE") or _dt.date.today().isoformat(),
              "request": card["request"], "host": card["host"], "auth": auth.record() if auth is not None else None,
              "provenance": {"library_ref": entry["id"], "library_rev": entry.get("rev"), "credential": card["credential"], "host": card["host"],
                             "attribution_text": (entry.get("license") or {}).get("attribution_text")},
              "rows": int(len(df)), "columns": list(df.columns), "sha256": hashlib.sha256(out.read_bytes()).hexdigest(), "calls": http.audit.calls, "notes": notes}
    import redact
    record, _ = redact.scrub_obj(record)
    rec_path.write_text(json.dumps(record, indent=2, default=str))
    return {"id": entry["id"], "rev": entry.get("rev"), "variant": variant, "level": level, "path": str(out), "record": str(rec_path), "cached": False,
            "rows": int(len(df)), "columns": list(df.columns), "run_card": card, "notes": notes, "geometry": False,
            "join": "a table without geometry: join it to a boundary layer on GEOID (table_join)"}


def find_entry(entry_id: str) -> dict:
    import ref_library as rl
    hits = [e for e in rl.all_entries("all") if e.get("id") == entry_id]
    if not hits:
        raise FetchError(f"no entry '{entry_id}'", gc.EXIT_USAGE)
    return hits[-1] if len(hits) > 1 and hits[-1].get("_scope") == "local" else hits[0]


TOOLS = ("census_query", "fcc_bdc", "hist_fetch", "cds_retrieve", "overture_io", "geoboundaries")


def fetch_by_tool(entry: dict, params: dict, *, refresh: bool = False, http=None, store=None) -> dict:
    """1.3.0: an entry whose delivery names a tool (`delivery.tool`) is fetched by that tool: census_query (ACS tables by variable), fcc_bdc (the BDC
    summary). `params` are the tool's ({level, state, ...}); the answer is the tool's (a cached table's path, rows, columns, findings)."""
    t = (entry.get("delivery") or {}).get("tool")
    if entry.get("status") not in ("active",):
        raise FetchError(f"{entry['id']} is {entry.get('status')}; only active entries are fetched", gc.EXIT_REFUSED)
    p = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    try:
        if t == "census_query":
            import census_query as cq
            return cq.from_library(entry, p, refresh=refresh, store=store, http=http)
        if t == "fcc_bdc":
            import fcc_bdc as fb
            return fb.from_library(entry, p, store=store, http=http, refresh=refresh)
        if t == "geoboundaries":                    # 1.5.0: gbOpen ADM1 or ADM2 for one country, CC BY 4.0 or more open only
            import geoboundaries as gb
            return gb.from_library(entry, p, refresh=refresh, http=http, store=store)
        if t == "overture_io":                      # 1.5.0: one z8 tile of detailed ground a variant (ground_detail.py prepare names them)
            import overture_io as ov
            return ov.from_library(entry, p, refresh=refresh, http=http, store=store)
    except Exception as ex:  # noqa: BLE001 - the tool's own error, with its rule
        rule = getattr(ex, "rule", None)
        raise FetchError(str(ex), getattr(ex, "code", gc.EXIT_SERVICE) if isinstance(getattr(ex, "code", None), int) else gc.EXIT_SERVICE, rule) from None
    raise FetchError(f"{entry['id']} is fetched with {t}: use that tool (hist_fetch.py fetch, cds_retrieve.py plan/submit/fetch); it needs a date, a box or a "
                     "request that ref_fetch does not take", gc.EXIT_USAGE)


def fetch_entry(entry_id: str, variant: str | None = None, level: str | None = None, refresh: bool = False, http=None, store=None) -> dict:
    """One library fetch by id, whatever its mode (the host-side MCP tool and the CLI share this)."""
    e = find_entry(entry_id)
    d = e.get("delivery") or {}
    if d.get("tool") in TOOLS and e.get("type") != "public-service":
        return fetch_by_tool(e, {"level": level, "state": variant}, refresh=refresh, http=http, store=store)
    if d.get("mode") == "service" and d.get("protocol") == "rest-json":
        return fetch_service(e, variant, level, refresh, http=http, store=store)
    if level:
        raise FetchError(f"{entry_id} takes no --level", gc.EXIT_USAGE)
    return fetch(e, variant, refresh, http=http, store=store)


FORMAT_WORDS = (("shapefile", ".zip"), ("geopackage", ".gpkg"), ("gpkg", ".gpkg"), ("geojson", ".geojson"), ("topojson", ".topojson"),
                ("parquet", ".parquet"), ("flatgeobuf", ".fgb"), ("kmz", ".kmz"), ("kml", ".kml"), ("gpx", ".gpx"), ("csv", ".csv"), ("tsv", ".tsv"),
                ("json", ".json"), ("zip", ".zip"))
CONTENT_TYPES = {"application/zip": ".zip", "application/x-zip-compressed": ".zip", "application/geo+json": ".geojson", "application/json": ".json",
                 "application/geopackage+sqlite3": ".gpkg", "application/x-sqlite3": ".gpkg", "text/csv": ".csv", "text/tab-separated-values": ".tsv",
                 "application/vnd.apache.parquet": ".parquet", "application/vnd.google-earth.kml+xml": ".kml", "application/vnd.google-earth.kmz": ".kmz",
                 "application/gpx+xml": ".gpx", "application/vnd.flatgeobuf": ".fgb"}
KNOWN_SUFFIXES = (".zip", ".gpkg", ".geojson", ".json", ".topojson", ".parquet", ".geoparquet", ".fgb", ".kml", ".kmz", ".gpx", ".csv", ".tsv", ".shp")


def sniff_suffix(head: bytes) -> str | None:
    """The file type from its first bytes, which is the one thing a publisher cannot mislabel: zip, SQLite (GeoPackage), Parquet,
    FlatGeobuf, JSON, XML (KML or GPX) or delimited text."""
    if head.startswith(b"PK\x03\x04"):
        return ".zip"
    if head.startswith(b"SQLite format 3\x00"):
        return ".gpkg"
    if head.startswith(b"PAR1"):
        return ".parquet"
    if head.startswith(b"fgb"):
        return ".fgb"
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if text[:1] in (b"{", b"["):
        return ".json"
    if text[:1] == b"<":
        low = text[:2048].lower()
        return ".gpx" if b"<gpx" in low else ".kml"
    try:
        s = text.decode("utf-8")
    except UnicodeDecodeError:
        try:
            s = text.decode("cp1252")
        except UnicodeDecodeError:
            return None
    first = s.splitlines()[0] if s.splitlines() else ""
    if any(d in first for d in (",", ";", "\t", "|")):
        return ".csv"
    return None


def choose_suffix(url: str, format_text: str | None, content_type: str | None, head: bytes) -> tuple[str, list[str]]:
    """The suffix the download is read under, and notes. The bytes decide; the entry's `delivery.format`, the URL and the Content-Type
    only break ties among text files and are reported when they disagree with the bytes."""
    notes = []
    from_bytes = sniff_suffix(head)
    path_suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    from_url = path_suffix if path_suffix in KNOWN_SUFFIXES else None
    from_format = None
    low = (format_text or "").lower()
    for word, suf in FORMAT_WORDS:
        if word in low:
            from_format = suf
            break
    from_ctype = None
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        from_ctype = CONTENT_TYPES.get(ct)
    stated = from_format or from_url or from_ctype
    if from_bytes is None:
        if stated is None:
            raise FetchError("the download is not a file type this plugin reads (not a zip, GeoPackage, Parquet, FlatGeobuf, JSON, KML, GPX or CSV)", gc.EXIT_LINT)
        return stated, notes
    if from_bytes == ".json" and stated in (".geojson", ".json", ".topojson"):
        return stated, notes
    if from_bytes == ".csv" and stated in (".csv", ".tsv"):
        return stated, notes
    if stated and stated != from_bytes and not (stated == ".zip" and from_bytes == ".zip"):
        notes.append(f"the entry says {format_text or from_url or content_type} but the download is a {from_bytes[1:]} file; it was read as what it is")
    return from_bytes, notes


# ---------------------------------------------------------------------------------------------------------------- M13: gzip and declared layouts
def gunzip(src: Path, dest: Path, limit: int = MAX_DECOMPRESSED) -> Path:
    """Decompress a gzip file to dest, stopping at `limit` bytes uncompressed."""
    import gzip
    total = 0
    with gzip.open(src, "rb") as g, open(dest, "wb") as f:
        while True:
            try:
                chunk = g.read(1 << 20)
            except (OSError, EOFError) as ex:
                raise FetchError(f"the gzip download is damaged: {ex}", gc.EXIT_LINT)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise FetchError(f"the gzip download holds more than {limit // (1024 * 1024)} MB; stopped", gc.EXIT_REFUSED)
            f.write(chunk)
    return dest


def has_geo_metadata(path: Path) -> bool:
    """A Parquet file with GeoParquet metadata (the `geo` key) is read by geo_io; a plain one needs `delivery.geometry_columns`."""
    try:
        import pyarrow.parquet as pq
        md = pq.read_schema(path).metadata or {}
        return b"geo" in md
    except Exception:  # noqa: BLE001 - unreadable here; geo_io reports it properly
        return True


def declares_layout(d: dict, suffix: str, path: Path | None = None) -> bool:
    """Whether the entry's own declaration decides how the file is read (rather than geo_io's guess)."""
    if d.get("header") is False or d.get("delimiter"):
        return True
    if d.get("geometry_columns") and suffix in (".csv", ".tsv", ".parquet", ".zip"):
        return True
    return bool(suffix == ".parquet" and path is not None and not has_geo_metadata(path))


def _delimiter(d: dict, name: str) -> str:
    raw = d.get("delimiter")
    if raw is None:
        return "\t" if Path(name).suffix.lower() in (".tsv", ".tab", ".txt") or d.get("header") is False else ","
    if str(raw) not in DELIMITERS and len(str(raw)) != 1:
        raise FetchError(f"delivery.delimiter '{raw}' is not one character (tab, comma, pipe, semicolon)", gc.EXIT_USAGE)
    return DELIMITERS.get(str(raw), str(raw))


def _zip_member(src: Path, d: dict, work: Path) -> Path:
    import zipfile
    with zipfile.ZipFile(src) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        want = d.get("member")
        if want:
            pick = [i for i in infos if i.filename == want or Path(i.filename).name == want]
        else:
            pick = [i for i in infos if Path(i.filename).suffix.lower() in TEXT_MEMBERS + (".parquet",)]
        if len(pick) != 1:
            names = ", ".join(i.filename for i in infos[:8])
            raise FetchError(f"the zip holds {len(infos)} file(s) ({names}); name the one to read in delivery.member", gc.EXIT_USAGE)
        info = pick[0]
        if info.file_size > MAX_DECOMPRESSED:
            raise FetchError(f"{info.filename} is {info.file_size // (1024 * 1024)} MB uncompressed; more than this reader takes", gc.EXIT_REFUSED)
        out = work / ("member" + (Path(info.filename).suffix.lower() or ".txt"))
        with z.open(info) as r, open(out, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        return out


def geometry_from(df, gcols: dict | None, crs: str | None = None):
    """(GeoDataFrame or the table unchanged, rows without geometry). `gcols` is {lon, lat} | {wkt} | {wkb}; no declaration gives the table."""
    import geopandas as gpd
    import numpy as np
    import pandas as pd
    import shapely
    if not gcols:
        return df, None
    crs = crs or "EPSG:4326"
    if gcols.get("lon") and gcols.get("lat"):
        miss = [c for c in (gcols["lon"], gcols["lat"]) if c not in df.columns]
        if miss:
            raise FetchError(f"delivery.geometry_columns names {', '.join(miss)}, which the file does not have", gc.EXIT_LINT)
        x = pd.to_numeric(df[gcols["lon"]], errors="coerce").to_numpy(dtype=float)
        y = pd.to_numeric(df[gcols["lat"]], errors="coerce").to_numpy(dtype=float)
        ok = np.isfinite(x) & np.isfinite(y) & (np.abs(x) <= 180) & (np.abs(y) <= 90)
        geom = np.array([None] * len(df), dtype=object)
        if ok.any():
            geom[ok] = shapely.points(x[ok], y[ok])
    elif gcols.get("wkt") or gcols.get("wkb"):
        col = gcols.get("wkt") or gcols.get("wkb")
        if col not in df.columns:
            raise FetchError(f"delivery.geometry_columns names {col}, which the file does not have", gc.EXIT_LINT)
        vals = df[col]
        if gcols.get("wkt"):
            geom = shapely.from_wkt(vals.where(vals.astype(str).str.strip() != "", None).to_numpy(dtype=object), on_invalid="ignore")
        else:
            v = vals.to_numpy(dtype=object)
            v = np.array([bytes.fromhex(s) if isinstance(s, str) and s else s for s in v], dtype=object)
            geom = shapely.from_wkb(v, on_invalid="ignore")
        df = df.drop(columns=[col])
    else:
        raise FetchError("delivery.geometry_columns is {lon, lat}, {wkt} or {wkb}", gc.EXIT_USAGE)
    g = gpd.GeoDataFrame(df.reset_index(drop=True), geometry=gpd.GeoSeries(list(geom), crs=crs), crs=crs)
    return g, int(g.geometry.isna().sum())


def read_declared(d: dict, src: Path, work: Path | None = None) -> tuple[object, bool, str, list[str]]:
    """Read a file by the entry's declared layout. -> (frame, table_only, format read, notes)."""
    import pandas as pd
    notes: list[str] = []
    work = work or src.parent
    path = src
    if src.suffix.lower() == ".zip":
        path = _zip_member(src, d, work)
        notes.append(f"read {d.get('member') or 'the one data file'} from the zip")
    suf = path.suffix.lower()
    if suf == ".parquet":
        df = pd.read_parquet(path)
        fmt = "parquet"
    else:
        import csv
        header = d.get("header", True) is not False
        cols = d.get("columns") or []
        if not header and not cols:
            raise FetchError("a file without a header row needs delivery.columns: the column names in order", gc.EXIT_USAGE)
        sep = _delimiter(d, path.name)
        df = pd.read_csv(path, sep=sep, header=0 if header else None, dtype=str, keep_default_na=False, index_col=False,
                         quoting=csv.QUOTE_NONE if sep == "\t" else csv.QUOTE_MINIMAL, encoding="utf-8-sig", engine="c")
        if not header:
            if len(df.columns) != len(cols):
                raise FetchError(f"the file has {len(df.columns)} columns; the entry declares {len(cols)}", gc.EXIT_LINT)
            df.columns = [str(c) for c in cols]
        fmt = "delimited text" + ("" if header else " without a header row")
    gcols = d.get("geometry_columns")
    out, missing = geometry_from(df, gcols, d.get("geometry_crs"))
    if gcols is None:
        notes.append("no geometry columns are declared; cached as a table without geometry (for joins), not as a layer")
        return out, True, fmt, notes
    if missing:
        notes.append(f"{missing} of {len(out)} row(s) carry no usable coordinates; they are kept as rows without geometry")
    return out, False, fmt, notes


def quadkey_prefixes(bbox, max_tiles: int = 64) -> list[str]:
    """The Bing-style quadkeys at the deepest zoom (at most 16) whose tiles, at most `max_tiles`, cover the lon/lat box."""
    import math

    def tile(lon, lat, z):
        lat = max(-85.0511, min(85.0511, lat))
        n = 2 ** z
        x = int((lon + 180.0) / 360.0 * n)
        y = int((1.0 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2.0 * n)
        return min(max(x, 0), n - 1), min(max(y, 0), n - 1)

    def qk(x, y, z):
        return "".join(str(((x >> (i - 1)) & 1) + 2 * ((y >> (i - 1)) & 1)) for i in range(z, 0, -1))

    w, s, e, n_ = (float(v) for v in bbox)
    best = [""]
    for z in range(1, 17):
        x0, y0 = tile(w, n_, z)
        x1, y1 = tile(e, s, z)
        if (x1 - x0 + 1) * (y1 - y0 + 1) > max_tiles:
            break
        best = [qk(x, y, z) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    return best


def read_user_file(entry: dict, path: Path, bbox=None) -> tuple[object, list[str]]:
    """A file the person supplied for a user-supplied entry, read by the entry's declared layout (or by geo_io when it declares none); a box
    keeps what intersects it (with `delivery.quadkey_column`, a Parquet is first cut to the quadkeys over the box, so the world is not parsed)."""
    import geo_io
    d = entry.get("delivery") or {}
    suffix = path.suffix.lower()
    notes: list[str] = []
    if suffix == ".gz":
        tmp = Path(tempfile.mkdtemp(prefix="gmap-read-"))
        inner = gunzip(path, tmp / Path(path.stem).name)
        path, suffix = inner, inner.suffix.lower()
        notes.append("decompressed the gzip file")
    if bbox and suffix == ".parquet" and d.get("quadkey_column"):
        import pyarrow.compute as pc
        import pyarrow.parquet as pq
        col = d["quadkey_column"]
        pre = quadkey_prefixes(bbox)
        t = pq.read_table(path)
        if col not in t.column_names:
            raise FetchError(f"delivery.quadkey_column '{col}' is not in the file", gc.EXIT_LINT)
        qcol = pc.cast(t[col], "string")
        mask = None
        for p_ in pre:
            m = pc.starts_with(qcol, p_)
            mask = m if mask is None else pc.or_(mask, m)
        t = t.filter(mask) if mask is not None else t
        tmp = Path(tempfile.mkdtemp(prefix="gmap-read-"))
        cut = tmp / "cut.parquet"
        pq.write_table(t, cut)
        notes.append(f"kept the {t.num_rows} row(s) whose {col} starts with one of {len(pre)} quadkey(s) over the box")
        path = cut
    if declares_layout(d, suffix, path):
        g, table_only, _fmt, more = read_declared(d, path)
        notes += more
    else:
        r = geo_io.read(path, layer=d.get("layer"))
        g, table_only = r.gdf, False
    if bbox and not table_only:
        import shapely
        box = shapely.box(*[float(v) for v in bbox])
        gg = g if g.crs is None or g.crs.equals("EPSG:4326") else g.to_crs("EPSG:4326")
        keep = gg.geometry.notna() & gg.geometry.intersects(box)
        notes.append(f"kept {int(keep.sum())} of {len(g)} feature(s) inside the box")
        g = g[keep.to_numpy()].copy()
    return g, notes


def fetch(entry: dict, variant: str | None = None, refresh: bool = False, opener=None, http=None, store=None) -> dict:
    """Download the entry's file, verify it, read it whatever its format and cache it as GeoParquet.

    The format comes from the bytes (see sniff_suffix), checked against `delivery.format`, the URL and the Content-Type; a zip is read by what
    it holds (a shapefile, several shapefiles, a GeoJSON, a GeoPackage, a CSV: geo_io decides). A CSV with no coordinate columns is a table,
    not a layer: it is cached as plain Parquet with no geometry, and the record says so, so a join can use it.
    `http` (a SafeHTTP) sends the download through the public-address, byte and time caps (the MCP tool's route); an entry that needs a
    credential always goes that way, with the key added at send time."""
    import geo_io
    if entry.get("status") != "active":
        raise FetchError(f"{entry['id']} is {entry.get('status')}; only active entries are fetched", gc.EXIT_REFUSED)
    if (entry.get("delivery") or {}).get("mode") != "fetch":
        raise FetchError(f"{entry['id']} is delivered as {(entry.get('delivery') or {}).get('mode')}, not by fetch", gc.EXIT_USAGE)
    if not (entry.get("url_verified_on")):
        raise FetchError(f"{entry['id']} has no verified URL", gc.EXIT_REFUSED)
    url, hash_applies = source_url(entry, variant)
    out = cache_dir() / cache_name(entry["id"], variant)
    if out.is_file() and not refresh:
        return {"id": entry["id"], "rev": entry.get("rev"), "variant": variant, "path": str(out), "cached": True, "url": url}
    out.parent.mkdir(parents=True, exist_ok=True)
    import credentials as cr
    auth = credential_for(entry, url, store) if cr.entry_block(entry) else None
    if auth is not None and http is None:
        import ogc_http as oh
        http = oh.SafeHTTP(limits={"timeout_s": 120, "max_bytes": oh.CEILING["max_bytes"]})
    tmp = Path(tempfile.mkdtemp(prefix="gmap-fetch-"))
    try:
        raw = tmp / "download.bin"
        try:
            sha, ctype = download_safe(url, raw, http, auth) if http is not None else download_with_type(url, raw, opener)
        finally:
            if auth is not None:
                auth.close()
        d = entry.get("delivery") or {}
        rolling = d.get("rolling") is True
        notes: list[str] = []
        want = (entry.get("source") or {}).get("file_sha256")
        if hash_applies and want and sha != want:
            if not rolling:
                raise FetchError(f"the downloaded file does not match the hash in {entry['id']} (got {sha[:12]}…, expected {want[:12]}…); the publisher may have replaced it. "
                                 "Not used: run ref-library verify, or ask for the entry to be refreshed", gc.EXIT_LINT)
            notes.append(f"the publisher rewrites this file in place (delivery.rolling); today's copy (SHA-256 {sha[:12]}…) differs from the one verified on "
                         f"{entry.get('url_verified_on')} ({want[:12]}…) and was used")
        with open(raw, "rb") as f:
            head = f.read(4096)
        name_url = url
        if head.startswith(b"\x1f\x8b"):                       # gzip (M13): read what it holds
            raw = gunzip(raw, tmp / "download.unz")
            with open(raw, "rb") as f:
                head = f.read(4096)
            name_url = re.sub(r"\.gz$", "", url.split("?")[0], flags=re.I)
            ctype = None
            notes.append("the download is gzip-compressed; it was decompressed and read as what it holds")
        suffix, more = choose_suffix(name_url, d.get("format"), ctype, head)
        notes += more
        src = tmp / f"download{suffix}"
        raw.rename(src)
        table_only = False
        try:
            if declares_layout(d, suffix, src):                # header-less text, several coordinate pairs, a plain Parquet (M13)
                gdf, table_only, fmt_read, more = read_declared(d, src, tmp)
                notes += more
            else:
                res = geo_io.read(src, layer=d.get("layer"))
                gdf = res.gdf
                notes += [n.msg for n in res.notes if n.sev == "WARN" or n.id == "IO"]
                fmt_read = res.format
        except geo_io.AmbiguousColumns:
            if suffix not in (".csv", ".tsv"):
                raise
            import pandas as pd
            gdf = pd.read_csv(src, dtype=str, sep=None, engine="python", encoding="utf-8-sig")
            table_only, fmt_read = True, "csv"
            notes.append("the CSV has no coordinate columns; cached as a table without geometry (for joins), not as a layer")
        except geo_io.GeoIOError as ex:
            raise FetchError(f"{entry['id']}: the download could not be read as {suffix[1:]}: {ex}", gc.EXIT_LINT)
        exp = d.get("features")
        if hash_applies and exp and len(gdf) != exp:
            tol = float(d.get("features_tolerance_pct", 5)) / 100.0 if rolling else 0.0
            if abs(len(gdf) - exp) > tol * exp:
                raise FetchError(f"{entry['id']}: {len(gdf)} features read, the entry says {exp}", gc.EXIT_LINT)
            notes.append(f"{len(gdf)} features read; the entry recorded {exp} on {entry.get('url_verified_on')} (a rolling file, within {tol * 100:g} percent)")
        if table_only:
            gdf.to_parquet(out, index=False)
        else:
            geo_io.write(gdf, out, "geoparquet")
        return {"id": entry["id"], "rev": entry.get("rev"), "variant": variant, "path": str(out), "cached": False, "url": url, "sha256": sha,
                "hash_checked": bool(hash_applies and want), "features": int(len(gdf)), "crs": None if table_only else str(gdf.crs), "notes": notes,
                "format": fmt_read, "content_type": ctype, "geometry": not table_only}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def read_main(e: dict, a) -> int:
    """`ref_fetch.py read ID FILE`: the person's own copy of a user-supplied (or any) entry, read by its declared layout into a layer file."""
    import geo_io
    if not a.file:
        print("read needs the file you supplied: ref_fetch.py read ENTRY_ID FILE", file=sys.stderr)
        return gc.EXIT_USAGE
    src = Path(a.file).expanduser()
    if not src.is_file():
        print(f"no such file: {src}", file=sys.stderr)
        return gc.EXIT_USAGE
    bbox = [float(v) for v in a.bbox.split(",")] if a.bbox else None
    if bbox is not None and len(bbox) != 4:
        print("--bbox is four numbers: W,S,E,N", file=sys.stderr)
        return gc.EXIT_USAGE
    g, notes = read_user_file(e, src, bbox)
    out = Path(a.out).expanduser() if a.out else src.with_name(re.sub(r"\.(gz|zip)$", "", src.name).split(".")[0] + "-read.parquet")
    if hasattr(g, "geometry") and getattr(g, "crs", None) is not None:
        geo_io.write(g, out, "geoparquet" if out.suffix.lower() == ".parquet" else None)
    else:
        g.to_parquet(out, index=False)
    lic = (e.get("license") or {})
    print(json.dumps({"id": e["id"], "file": src.name, "out": str(out), "rows": int(len(g)), "notes": notes, "license": lic.get("class"),
                      "attribution": lic.get("attribution_text"), "draw_it": f"source: {{type: file, path: {out.name}}} with provenance from {e['id']}"}, indent=2))
    return gc.EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["fetch", "status", "read", "plan"])
    ap.add_argument("id")
    ap.add_argument("file", nargs="?", help="read: the file you supplied for the entry")
    ap.add_argument("--variant")
    ap.add_argument("--level", help="a service entry's level (state, county, tract, \"block group\"), from its delivery.requests")
    ap.add_argument("--credentials-dir", help="the credential folder, when it is connected to this session (the fallback route)")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--out", help="read: where to write the layer (GeoParquet, GeoPackage or GeoJSON by extension); default <file>-read.parquet beside it")
    ap.add_argument("--bbox", help="read: keep what intersects W,S,E,N (write --bbox=-112.4,33.2,-111.6,33.8)")
    a = ap.parse_args(argv)
    import credentials as cr
    try:
        e = find_entry(a.id)
    except FetchError as ex:
        print(str(ex), file=sys.stderr)
        return ex.code
    with cr.value_scope() as vs:
        try:
            if a.op == "status":
                tag = "-".join(x for x in (a.level, a.variant) if x) or None
                p = cached_path(e, tag)
                print(json.dumps({"id": a.id, "cached": bool(p), "path": str(p) if p else None}, indent=2))
                return gc.EXIT_OK
            if a.op == "read":
                return read_main(e, a)
            if a.op == "plan":
                print(json.dumps(run_card(e, a.variant, a.level), indent=2))
                return gc.EXIT_OK
            store = cr.Store(a.credentials_dir) if a.credentials_dir else None
            print(json.dumps(vs.clean(fetch_entry(a.id, a.variant, a.level, a.refresh, store=store)), indent=2, default=str))
            return gc.EXIT_OK
        except FetchError as ex:
            print(f"ref_fetch: {vs.clean(str(ex))}" + (f" ({ex.rule})" if ex.rule else ""), file=sys.stderr)
            return ex.code


if __name__ == "__main__":
    sys.exit(main())
