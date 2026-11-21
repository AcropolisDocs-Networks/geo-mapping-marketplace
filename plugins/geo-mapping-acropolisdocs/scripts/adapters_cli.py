#!/usr/bin/env python3
"""adapters_cli: the data adapters from the command line (3.1.0, G-11, G-12).

    adapters_cli.py list --region "Bavaria"                                  every adapter, by how much of the region it covers
    adapters_cli.py plan bnetza-mobile-coverage --region DE-BY               the files and their size (the cost card); nothing fetched
    adapters_cli.py fetch arcep-mon-reseau-mobile --region FR-IDF --out data/arcep-mon-reseau-mobile --confirm [--opt technology=4G]
    adapters_cli.py ofcom-postcodes sites.csv --column postcode --out coverage.csv      (the person's ofcom-mobile key)

A fetch without --confirm prints the cost card only. Then build the map with `source: {type: h3, adapter: NAME}` and a region: block.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gmap_common as gc  # noqa: E402


def _opts(pairs) -> dict:
    out = {}
    for kv in pairs or []:
        k, _, v = str(kv).partition("=")
        if k:
            out[k.strip()] = v.strip()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="op", required=True)
    for name in ("list", "plan", "fetch"):
        sp = sub.add_parser(name)
        if name != "list":
            sp.add_argument("adapter")
        sp.add_argument("--region", required=True)
        sp.add_argument("--opt", action="append", help="key=value (technology=5G, use=data, environment=outdoor, kind=mobile)")
        if name == "fetch":
            sp.add_argument("--out", required=True)
            sp.add_argument("--confirm", action="store_true")
    pc = sub.add_parser("ofcom-postcodes")
    pc.add_argument("table")
    pc.add_argument("--column", default="postcode")
    pc.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    import adapters
    import region as region_mod
    import service_tools as st
    if a.op == "ofcom-postcodes":
        import pandas as pd
        t = pd.read_csv(a.table)
        r = st.ofcom_postcodes_tool(",".join(str(x) for x in t[a.column].dropna()))
        if not r.get("ok"):
            print(json.dumps(r, indent=2))
            return gc.EXIT_REFUSED
        pd.DataFrame(r["rows"]).to_csv(a.out, index=False)
        gc.ledger_note(outputs=[a.out])
        print(json.dumps({"ok": True, "out": a.out, "postcodes": r["postcodes"], "attribution": r["attribution"]}, indent=2))
        return gc.EXIT_OK
    try:
        reg = st.region_of_text(a.region)
    except region_mod.RegionError as ex:
        print(json.dumps({"ok": False, "findings": [{"id": ex.rule, "severity": "BLOCK", "message": str(ex)}]}, indent=2))
        return ex.code
    if a.op == "list":
        print(json.dumps({"region": reg.name, "adapters": adapters.ranked(reg)}, indent=2, default=str))
        return gc.EXIT_OK
    try:
        ad = adapters.get(a.adapter)
        folder = Path(a.out) if a.op == "fetch" else gc.data_dir(ad.name)
        r = ad.fetch(reg, folder, confirm=(a.op == "fetch" and a.confirm), **_opts(a.opt))
    except adapters.AdapterError as ex:
        print(json.dumps({"ok": False, "findings": [{"id": ex.rule, "severity": "BLOCK", "message": str(ex)}]}, indent=2))
        return ex.code
    if r.get("files"):
        gc.ledger_note(outputs=[f["path"] for f in r["files"]])
    print(json.dumps(r, indent=2, default=str))
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
