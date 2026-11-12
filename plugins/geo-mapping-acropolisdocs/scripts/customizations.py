#!/usr/bin/env python3
"""customizations: the one acknowledgment a person makes when they go beyond the plugin's defaults (0.23.0).

The defaults need nothing from anyone: the bundled background, the bundled reference layers, the US Census geocoder, USGS 3DEP elevation in
the United States and the AWS terrain tiles elsewhere (their credit is printed for you), the FOSSGIS Valhalla router within its limits,
transit times from your own GTFS feed, and the public services the reference library lists as active. The plugin keeps each of them within
its provider's terms by itself: request limits, tile caps, the credit on the map.

A customization is a tool, API, tile provider or data service you add or choose that is not a default: OpenStreetMap's own tile servers,
Nominatim geocoding, a free tier that is for non-commercial use, a key you register, a data service you save. Those terms are yours, and you
accepted them when you signed up or got your key. The plugin does not ask you to accept them again, and it does not hide a terms switch in a
map's settings. It asks once, in plain words, that you keep each one within the terms you accepted (STATEMENT), and records that you said yes
here: `.geo-mapping/customizations.yaml`. A key you register is itself that acceptance for its service.

    customizations.py status [--json]          defaults, your acknowledgment (when and how), the keys and services you added
    customizations.py acknowledge --yes        record it; run only after the person said yes to STATEMENT in this conversation
    customizations.py withdraw                 remove it; customizations without a key are refused again until it is made

Older MapSpecs that say `terms_acknowledged: true` keep building: the line counts as this acknowledgment for that one map. New MapSpecs never
need it. Nothing here lifts a refusal that is not about terms: blocked providers (Google, Bing), entries whose terms are not read yet, the
privacy gate, missing keys and every cap stay as they are.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

FILE = "customizations.yaml"
STATEMENT_VERSION = 1
STATEMENT = ("I am using tools, APIs, tile providers or data services beyond the map plugin's defaults. For each one I add or choose, I will keep "
             "my use within that provider's terms and my plan with it, the terms I accepted when I signed up or got my key. The plugin keeps its "
             "own safeguards (request limits, tile caps, the credit on the map) but does not check my plan or licence.")
DEFAULTS = ("the bundled background and reference layers", "the US Census geocoder", "USGS 3DEP elevation in the US, and the AWS terrain tiles "
            "elsewhere with their credit printed", "the FOSSGIS Valhalla router within its limits", "transit times from your own GTFS feed",
            "public services the reference library lists as active")
HOWS = ("asked", "key added", "older mapspec")


def _candidates() -> list[Path]:
    """Where an acknowledgment may be: the working folder's .geo-mapping/, the folder a tool call named, and (outside tests) the person's own
    ~/.geo-mapping/, which is where the plugin's server on their computer keeps settings."""
    out = [gc.workdir() / FILE, gc.outputs_dir() / FILE]
    if not os.environ.get("GEO_MAPPING_HOME"):
        out.append(Path.home() / gc.WORKDIR_NAME / FILE)
    seen, uniq = set(), []
    for p in out:
        k = str(p.resolve()) if p.exists() else str(p)
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def _read(p: Path) -> dict | None:
    try:
        d = gc.load_yaml(p) or {}
    except Exception:  # noqa: BLE001 - an unreadable file is not an acknowledgment
        return None
    return d if isinstance(d, dict) and d.get("acknowledged") is True else None


def _keys_registered() -> list[str]:
    """Names of the keys registered in the person's credential folder, where this process can read it (never a value)."""
    try:
        import credentials as cr
        base = os.environ.get("GEO_MAPPING_HOME")             # tests and the persistence spike: only their own folder, never the real one
        st = cr.Store(Path(base) / "credentials") if base else cr.Store()
        return sorted(str(k) for k in (st.load().get("credentials") or {})) if st.exists() else []
    except Exception:  # noqa: BLE001 - no reachable credential folder means no keys from here
        return []


def status() -> dict:
    """{acknowledged, on, how, statement_version, where, keys, defaults, statement}. A registered key counts as the acknowledgment (how: key
    added) when no file says so: the person accepted the provider's terms to get it."""
    for p in _candidates():
        d = _read(p)
        if d:
            return {"acknowledged": True, "on": str(d.get("acknowledged_on") or ""), "how": str(d.get("how") or "asked"),
                    "statement_version": int(d.get("statement_version") or 1), "where": str(p), "keys": _keys_registered(),
                    "defaults": list(DEFAULTS), "statement": STATEMENT}
    keys = _keys_registered()
    if keys:
        return {"acknowledged": True, "on": "", "how": "key added", "statement_version": STATEMENT_VERSION, "where": "your credential folder",
                "keys": keys, "defaults": list(DEFAULTS), "statement": STATEMENT}
    return {"acknowledged": False, "on": "", "how": "", "statement_version": STATEMENT_VERSION, "where": "", "keys": [], "defaults": list(DEFAULTS),
            "statement": STATEMENT}


def acknowledged(explicit=None) -> bool:
    """True when the person made the acknowledgment, or an older MapSpec's own `terms_acknowledged: true` covers this one map."""
    if explicit is True or (isinstance(explicit, str) and explicit.strip().lower() in ("true", "yes", "1")):
        return True
    return bool(status()["acknowledged"])


def how(explicit=None) -> str:
    """What made it count, for the provenance record: asked, key added or older mapspec ("" when nothing)."""
    if explicit is True:
        return "older mapspec"
    s = status()
    return s["how"] if s["acknowledged"] else ""


def command() -> str:
    try:
        return f"/{gc.build_manifest()['plugin_name']}:map-services"
    except Exception:  # noqa: BLE001
        return "/geo-mapping-acropolisdocs:map-services"


def needed(what: str, default_hint: str = "") -> str:
    """The plain message of a refusal for want of the acknowledgment. `what` names the customization, e.g. "OpenStreetMap's tile servers"."""
    return (f"{what} is a customization beyond the plugin's defaults. Make the one acknowledgment first ({command()}): it is asked once, covers "
            f"every service you add or choose, and says you will keep each within the terms you accepted with that provider. Then build again"
            + (f"; or {default_hint}" if default_hint else ""))


def acknowledge(how_: str = "asked", where: Path | None = None) -> dict:
    """Write the acknowledgment (STATEMENT, today, how). Called only after the person said yes, or by credentials add (a key is acceptance)."""
    if how_ not in HOWS:
        raise ValueError(f"how is one of {', '.join(HOWS)}")
    import yaml
    p = where or (gc.workdir(create=True) / FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    today = (os.environ.get("GMAP_FIXED_DATE") or dt.date.today().isoformat())[:10]
    body = ("# Written by map-services when you said yes to the statement below. Delete this file, or run map-services withdraw, to take it back.\n"
            + yaml.safe_dump({"acknowledged": True, "acknowledged_on": today, "how": how_, "statement_version": STATEMENT_VERSION, "statement": STATEMENT}, sort_keys=False, width=200))
    p.write_text(body, encoding="utf-8")
    return {"acknowledged": True, "acknowledged_on": today, "how": how_, "stored_in": str(p)}


def withdraw() -> dict:
    gone = []
    for p in _candidates():
        if p.is_file():
            try:
                p.unlink()
                gone.append(str(p))
            except OSError:
                pass
    return {"withdrawn": gone, "acknowledged": status()["acknowledged"],
            "note": "a registered key still counts for its own service" if status()["acknowledged"] else ""}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    a_ = sub.add_parser("acknowledge")
    a_.add_argument("--yes", action="store_true", help="the person said yes to the statement in this conversation")
    sub.add_parser("withdraw")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        st = status()
        if a.json:
            print(json.dumps(st, indent=2))
        else:
            print("Defaults (nothing to acknowledge): " + "; ".join(DEFAULTS) + ".")
            if st["acknowledged"]:
                when = f" on {st['on']}" if st["on"] else ""
                print(f"Acknowledged{when} ({st['how']}). Customizations are on: keep each service within the terms you accepted with it.")
            else:
                print("Not acknowledged. Customizations without a key are refused until you say yes to this statement:\n  " + STATEMENT)
            if st["keys"]:
                print("Your registered keys (each is your own acceptance of its service's terms): " + ", ".join(st["keys"]))
        return gc.EXIT_OK
    if a.cmd == "acknowledge":
        if not a.yes:
            print("Show the person the statement and record it only after they say yes, with --yes:\n  " + STATEMENT, file=sys.stderr)
            return gc.EXIT_USAGE
        print(json.dumps(acknowledge("asked"), indent=2))
        return gc.EXIT_OK
    print(json.dumps(withdraw(), indent=2))
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
