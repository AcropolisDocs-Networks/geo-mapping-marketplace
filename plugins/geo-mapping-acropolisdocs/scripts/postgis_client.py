#!/usr/bin/env python3
"""postgis_client (0.15.0): read-only live connections to the person's own PostGIS database.

The login is a registered credential of kind postgres (credentials.py: host, port, database, user and sslmode in the registry, the password alone
in the private file). Only the plugin's MCP server on the person's computer opens the connection, so a database on their own network is reachable
and the password never leaves that computer. Every connection is read-only three ways: the session starts with default_transaction_read_only=on,
each transaction is opened READ ONLY, and `SHOW transaction_read_only` must answer `on` before anything is read (MAP-SVC-042). A statement timeout
and a lock timeout bound every query.

Nothing the model writes becomes SQL. A table is one listed in the database's own geometry_columns view; a column is one of that table's columns;
a condition is the plugin's simple form (STATE=AZ; POP>=1000; NAME~Phoe*) with the value always sent as a parameter (MAP-SVC-041). A fetch counts
first and refuses a result larger than its bound rather than cutting it short (MAP-SVC-004), then writes a GeoPackage with a capture record.

    postgis_client.py tables  --credential cred:NAME [--table schema.table] [--where "STATE=AZ"] [--bbox x0,y0,x1,y1]
    postgis_client.py fetch   --credential cred:NAME --table schema.table [--columns a,b] [--where ...] [--bbox ...] [--max-features N] [--out FILE]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

CONNECT_TIMEOUT_S = 10
STATEMENT_TIMEOUT_MS = 60_000
LOCK_TIMEOUT_MS = 5_000
DEFAULT_MAX = 50_000          # a fetch refuses a result larger than this unless max_features says otherwise
CEILING = 500_000             # and never more than this
BATCH = 5_000
OPS = {"=": "=", "!=": "<>", "<": "<", "<=": "<=", ">": ">", ">=": ">="}
QNAME_RE = re.compile(r"^(?:([A-Za-z_][A-Za-z0-9_$-]{0,62})\.)?([A-Za-z_][A-Za-z0-9_$-]{0,62})$")


class PgRefusal(Exception):
    def __init__(self, message: str, rule: str = "MAP-SVC-041"):
        super().__init__(message)
        self.rule = rule


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def now() -> str:
    fixed = os.environ.get("GMAP_FIXED_DATE")
    if fixed:
        return f"{fixed}T00:00:00Z"
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------------------------------------------- the connection
def _psycopg():
    try:
        import psycopg
    except ImportError as e:                      # SOFT dependency: map-doctor --fix installs it
        raise PgRefusal("reading a PostGIS database needs the psycopg package (map-doctor --fix installs it)", "MAP-SVC-042") from e
    return psycopg


def connect(cred):
    """A read-only connection for a resolved credential of kind postgres. The password is used here and nowhere else; every error message is masked."""
    psycopg = _psycopg()
    opts = (f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS} -c lock_timeout={LOCK_TIMEOUT_MS} "
            f"-c idle_in_transaction_session_timeout={STATEMENT_TIMEOUT_MS}")
    try:
        conn = psycopg.connect(**cred.pg_params(), connect_timeout=CONNECT_TIMEOUT_S, application_name="geo-mapping-plugin (read-only)", options=opts)
    except psycopg.OperationalError as e:
        msg = cred.mask(str(e).strip().splitlines()[0] if str(e).strip() else "the connection failed")
        state = getattr(getattr(e, "diag", None), "sqlstate", None) or ""
        if state in ("28P01", "28000") or "password authentication failed" in msg:
            cred.rejected("the database refused the login")
            raise PgRefusal(f"the database on {cred.host} refused the login '{cred.name}': {msg}", "MAP-CRED-008") from None
        raise PgRefusal(f"could not connect to the database on {cred.host}: {msg}", "MAP-SVC-042") from None
    conn.read_only = True
    try:
        with conn.cursor() as c:
            c.execute("SHOW transaction_read_only")
            ro = (c.fetchone() or [""])[0]
    except Exception as e:  # noqa: BLE001
        conn.close()
        raise PgRefusal("could not confirm the connection is read-only: " + cred.mask(str(e))[:200], "MAP-SVC-042") from None
    if ro != "on":
        conn.close()
        raise PgRefusal("the database did not accept a read-only session; nothing is read", "MAP-SVC-042")
    return conn


def probe(cred) -> dict:
    """map-credentials check --probe for a database login: one read-only connection and the PostGIS version."""
    try:
        conn = connect(cred)
    except PgRefusal as e:
        if e.rule == "MAP-CRED-008":
            return {"probed": True, "accepted": False, "findings": [finding("MAP-CRED-008", "BLOCK", str(e))]}
        return {"probed": True, "accepted": None, "findings": [finding(e.rule, "WARN", str(e))]}
    try:
        with conn.cursor() as c:
            try:
                c.execute("SELECT postgis_lib_version()")
                ver = c.fetchone()[0]
            except Exception:  # noqa: BLE001
                conn.rollback()
                ver = None
        out = {"probed": True, "accepted": True, "request": f"read-only connection to {cred.host} (+ password: ***)", "postgis": ver}
        if ver is None:
            out["findings"] = [finding("MAP-SVC-042", "WARN", "the login works, but this database has no PostGIS extension; its tables have no geometry to read")]
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------------------------- the catalog
def tables(conn) -> list[dict]:
    """The tables with a geometry column, from the database's own geometry_columns view, with an estimated row count."""
    with conn.cursor() as c:
        c.execute("""SELECT g.f_table_schema, g.f_table_name, g.f_geometry_column, g.srid, g.type, GREATEST(cl.reltuples, -1)::bigint
                     FROM geometry_columns g
                     LEFT JOIN pg_namespace n ON n.nspname = g.f_table_schema
                     LEFT JOIN pg_class cl ON cl.relnamespace = n.oid AND cl.relname = g.f_table_name
                     ORDER BY 1, 2, 3 LIMIT 2000""")
        rows = c.fetchall()
    conn.rollback()
    return [{"table": f"{s}.{t}", "geometry_column": g, "srid": int(srid or 0), "geometry_type": typ, "rows_estimate": (int(n) if n is not None and n >= 0 else None)}
            for s, t, g, srid, typ, n in rows]


def resolve_table(conn, name: str, default_schema: str = "public", geometry_column: str = "") -> dict:
    """A table the model named, matched to one row of geometry_columns (MAP-SVC-041 otherwise). The schema defaults to the login's schema."""
    m = QNAME_RE.match(name or "")
    if not m:
        raise PgRefusal(f"'{str(name)[:60]}' is not a table name (schema.table)")
    schema, table = m.group(1) or default_schema or "public", m.group(2)
    rows = [t for t in tables(conn) if t["table"] == f"{schema}.{table}"]
    if geometry_column:
        rows = [t for t in rows if t["geometry_column"] == geometry_column]
    if not rows:
        raise PgRefusal(f"{schema}.{table} is not a table with a geometry column in this database (postgis_tables lists them)")
    return {**rows[0], "schema": schema, "name": table}


def columns(conn, t: dict) -> list[dict]:
    with conn.cursor() as c:
        c.execute("""SELECT column_name, data_type, udt_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position""",
                  (t["schema"], t["name"]))
        rows = c.fetchall()
    conn.rollback()
    return [{"name": n, "type": (u if d == "USER-DEFINED" else d)} for n, d, u in rows]


def _primary_key(conn, t: dict) -> list[str]:
    from psycopg import sql
    with conn.cursor() as c:
        c.execute("""SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                     WHERE i.indrelid = %s::regclass AND i.indisprimary ORDER BY array_position(i.indkey, a.attnum)""",
                  (sql.Identifier(t["schema"], t["name"]).as_string(conn),))
        out = [r[0] for r in c.fetchall()]
    conn.rollback()
    return out


def _where(conn, t: dict, cols: list[dict], conditions, bbox) -> tuple:
    """The WHERE clause as composed SQL plus its parameters. Every name is checked against the table; every value is a parameter."""
    import ogc_client as oc
    from psycopg import sql
    names = {c["name"] for c in cols}
    parts, params = [], []
    try:
        conds = oc.parse_conditions(conditions)
    except oc.Refusal as e:
        raise PgRefusal(str(e), "MAP-SVC-038") from None
    for cd in conds:
        if cd["property"] not in names:
            raise PgRefusal(f"'{cd['property'][:60]}' is not a column of {t['table']}")
        if cd["property"] == t["geometry_column"]:
            raise PgRefusal("a condition cannot compare the geometry column; use bbox= for an area")
        col = sql.Identifier(cd["property"])
        if cd["op"] == "~":
            parts.append(sql.SQL("{}::text LIKE %s").format(col))
            params.append(cd["value"].replace("%", r"\%").replace("_", r"\_").replace("*", "%"))
        else:
            parts.append(sql.SQL("{} " + OPS[cd["op"]] + " %s").format(col))
            params.append(cd["value"])
    if bbox:
        x0, y0, x1, y1 = (float(v) for v in bbox)
        if not (-180 <= x0 < x1 <= 180 and -90 <= y0 < y1 <= 90):
            raise PgRefusal("bbox is minx,miny,maxx,maxy in longitude and latitude", "MAP-SVC-004")
        if not t["srid"]:
            raise PgRefusal(f"{t['table']} declares no SRID, so a lon/lat box cannot be placed on it")
        parts.append(sql.SQL("{} && ST_Transform(ST_MakeEnvelope(%s, %s, %s, %s, 4326), %s)").format(sql.Identifier(t["geometry_column"])))
        params += [x0, y0, x1, y1, t["srid"]]
    return (sql.SQL(" AND ").join(parts) if parts else sql.SQL("TRUE")), params


def describe(conn, name: str, *, default_schema: str = "public", conditions=None, bbox=None, geometry_column: str = "") -> dict:
    """A table's columns, geometry, SRID, estimated extent and, with conditions or a box, the exact count that a fetch would return."""
    from psycopg import sql
    t = resolve_table(conn, name, default_schema, geometry_column)
    cols = columns(conn, t)
    ext = None
    with conn.cursor() as c:
        try:
            c.execute(sql.SQL("SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) FROM (SELECT ST_Transform(ST_SetSRID(ST_EstimatedExtent(%s, %s, %s)::geometry, %s), 4326) AS e) s"),
                      (t["schema"], t["name"], t["geometry_column"], t["srid"] or 4326))
            r = c.fetchone()
            ext = [round(v, 6) for v in r] if r and r[0] is not None else None
        except Exception:  # noqa: BLE001  (no statistics yet: the extent is left out)
            conn.rollback()
        where, params = _where(conn, t, cols, conditions, bbox)
        c.execute(sql.SQL("SELECT count(*) FROM {} WHERE ").format(sql.Identifier(t["schema"], t["name"])) + where, params)
        n = c.fetchone()[0]
    conn.rollback()
    return {"table": t["table"], "geometry_column": t["geometry_column"], "geometry_type": t["geometry_type"], "srid": t["srid"],
            "crs": f"EPSG:{t['srid']}" if t["srid"] else None, "columns": [c for c in cols if c["name"] != t["geometry_column"]],
            "primary_key": _primary_key(conn, t), "extent_lonlat_estimate": ext, "count": int(n), "rows_estimate": t["rows_estimate"]}


# ---------------------------------------------------------------------------------------------------------------- the fetch
def fetch(conn, cred, name: str, out: Path, *, default_schema: str = "public", columns_wanted=None, conditions=None, bbox=None,
          max_features: int = 0, geometry_column: str = "") -> dict:
    """Count, then read the rows (in batches, ordered by the primary key) into a GeoPackage, then write the capture record beside it."""
    import geopandas as gpd
    import shapely
    from psycopg import sql
    started = now()
    t = resolve_table(conn, name, default_schema, geometry_column)
    cols = columns(conn, t)
    names = [c["name"] for c in cols if c["name"] != t["geometry_column"]]
    want = [c.strip() for c in (columns_wanted.split(",") if isinstance(columns_wanted, str) else columns_wanted or []) if c and c.strip()]
    bad = [c for c in want if c not in names]
    if bad:
        raise PgRefusal(f"not columns of {t['table']}: {', '.join(b[:40] for b in bad[:5])}")
    sel = want or names
    cap = max(1, min(int(max_features or DEFAULT_MAX), CEILING))
    where, params = _where(conn, t, cols, conditions, bbox)
    tbl = sql.Identifier(t["schema"], t["name"])
    with conn.cursor() as c:
        c.execute(sql.SQL("SELECT count(*) FROM {} WHERE ").format(tbl) + where, params)
        n = int(c.fetchone()[0])
    conn.rollback()
    if n > cap:
        raise PgRefusal(f"{n} rows match, more than the bound of {cap}; narrow it with bbox= or conditions, or raise max_features (at most {CEILING})", "MAP-SVC-004")
    pk = _primary_key(conn, t)
    order = sql.SQL(", ").join(sql.Identifier(k) for k in pk) if pk else sql.SQL("ctid")
    q = (sql.SQL("SELECT {cols}, ST_AsBinary({g}) FROM {t} WHERE ").format(cols=sql.SQL(", ").join(sql.Identifier(c) for c in sel) if sel else sql.SQL("1"),
                                                                          g=sql.Identifier(t["geometry_column"]), t=tbl)
         + where + sql.SQL(" ORDER BY ") + order)
    rows: list = []
    with conn.transaction():
        with conn.cursor(name="gm_fetch") as c:        # a server-side cursor: the rows arrive in batches, never all at once
            c.itersize = BATCH
            c.execute(q, params)
            for r in c:
                rows.append(r)
    conn.rollback()
    geoms = shapely.from_wkb([bytes(r[-1]) if r[-1] is not None else None for r in rows])
    data = {col: [r[i] for r in rows] for i, col in enumerate(sel)} if sel else {}
    gdf = gpd.GeoDataFrame(data, geometry=list(geoms), crs=f"EPSG:{t['srid']}" if t["srid"] else None)
    for col in gdf.columns:                             # Decimal, dates and the like are kept as text where GeoPackage has no type for them
        if col != "geometry" and gdf[col].dtype == object:
            gdf[col] = gdf[col].map(lambda v: v if v is None or isinstance(v, (str, int, float, bool)) else str(v))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    gdf.to_file(out, driver="GPKG", layer=t["name"])
    bb = None
    if len(gdf) and gdf.crs is not None:
        bb = [round(v, 6) for v in gdf.to_crs("EPSG:4326").total_bounds]
    rec = {"capture_version": 1, "kind": "postgis", "route": "database", "mode": "capture", "started_at": started, "finished_at": now(), "captured_at": now(),
           "host": cred.host, "database": cred.inject["postgres"]["database"], "auth": cred.record(), "protocol": "postgis",
           "request": {"table": t["table"], "columns": sel, "conditions": conditions or None, "bbox": list(bbox) if bbox else None, "max_features": cap},
           "hits_expected": n, "received": len(gdf), "truncated": len(gdf) != n, "read_only": True,
           "result": {"path": str(out), "format": "gpkg", "sha256": hashlib.sha256(out.read_bytes()).hexdigest(), "features": int(len(gdf)), "bbox": bb,
                      "crs": f"EPSG:{t['srid']}" if t["srid"] else None, "fields": sel},
           "license": {"profile_default": "internal", "note": "the person's own database"}, "confidential": True}
    fs = []
    if rec["truncated"]:
        fs.append(finding("MAP-SVC-005", "BLOCK", f"{len(gdf)} rows were read and {n} were counted (the table changed during the read?)"))
    rec["findings"] = fs
    rp = out.with_suffix(".capture.json")
    rp.write_text(json.dumps(rec, indent=2, default=str))
    return {"ok": not fs, "record": str(rp), "result": str(out), "features": len(gdf), "expected": n, "bbox": bb, "fields": sel, "crs": rec["result"]["crs"], "findings": fs}


# ---------------------------------------------------------------------------------------------------------------- one call, opened and closed
def run(credential: str, action: str, *, table: str = "", out: Path | None = None, columns_wanted=None, conditions=None, bbox=None, max_features: int = 0,
        geometry_column: str = "", store=None) -> dict:
    """Resolve the login, open one read-only connection, do one thing, close it. The result never holds the password."""
    import credentials as cr
    try:
        cred = cr.resolve(credential, store=store, purpose="database")
    except cr.CredentialError as e:
        return {"ok": False, "findings": e.findings}
    try:
        conn = connect(cred)
        try:
            schema = cred.inject["postgres"].get("schema") or "public"
            if action == "tables" and not table:
                return {"ok": True, "database": cred.inject["postgres"]["database"], "host": cred.host, "tables": tables(conn), "findings": cred.findings}
            if action == "tables":
                return {"ok": True, **describe(conn, table, default_schema=schema, conditions=conditions, bbox=bbox, geometry_column=geometry_column), "findings": cred.findings}
            if action == "fetch":
                r = fetch(conn, cred, table, out, default_schema=schema, columns_wanted=columns_wanted, conditions=conditions, bbox=bbox,
                          max_features=max_features, geometry_column=geometry_column)
                r["findings"] = cred.findings + r["findings"]
                return r
            return {"ok": False, "findings": [finding("MAP-SVC-041", "BLOCK", f"unknown action {action}")]}
        finally:
            conn.close()
    except PgRefusal as e:
        return {"ok": False, "findings": [finding(e.rule, "BLOCK", cred.mask(str(e)))]}
    except Exception as e:  # noqa: BLE001  (a database error: masked and reported, never raised with the password in it)
        state = getattr(getattr(e, "diag", None), "sqlstate", None)
        return {"ok": False, "findings": [finding("MAP-SVC-042", "BLOCK", f"the database answered with an error{f' ({state})' if state else ''}: " + cred.mask(str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__)[:300])]}
    finally:
        cred.close()


def main(argv=None) -> int:
    import gmap_common as gc
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("tables", "fetch"))
    ap.add_argument("--credential", required=True)
    ap.add_argument("--table", default="")
    ap.add_argument("--columns", default="")
    ap.add_argument("--where", default="")
    ap.add_argument("--bbox", default="")
    ap.add_argument("--max-features", type=int, default=0)
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    bb = [float(v) for v in a.bbox.split(",")] if a.bbox else None
    out = Path(a.out) if a.out else gc.outputs_dir(True) / "captures" / f"postgis-{re.sub(r'[^A-Za-z0-9_.-]', '_', a.table)}.gpkg"
    r = run(a.credential, a.action, table=a.table, out=out, columns_wanted=a.columns or None, conditions=a.where or None, bbox=bb, max_features=a.max_features)
    print(json.dumps(r, indent=2, default=str))
    return 0 if r.get("ok") else gc.EXIT_LINT


if __name__ == "__main__":
    sys.exit(main())
