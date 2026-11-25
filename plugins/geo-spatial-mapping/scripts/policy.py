#!/usr/bin/env python3
"""policy: the map plugin's standing rules as data, and the deviations table every build carries (4.2.0).

    policy.py show                  the fixed rules, the defaults and the free keys
    policy.py digest                the ten lines a map request gets once a session
    policy.py check MAPSPEC         the deviations of a MapSpec from the defaults, by box

The rules live in data/policy.yaml. A MapSpec that sets a default-class key to another value has deviated. A deviation is

    asked        the MapSpec's `asks:` names the key with the person's words:  asks: [{set: theme, words: "make it dark"}]
    intent       a named intent's bundle sets it (5.0.0)
    unexplained  neither

and a free-class key the MapSpec sets is counted as free. 4.2.0 reports and changes nothing: the table is in the build result
(`deviations`) and its one line in the digest. Standard library and PyYAML only; reaches no network.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

POLICY_FILE = HERE.parent / "data" / "policy.yaml"
_MISSING = object()
_CACHE: dict = {}


def load(path: Path | None = None) -> dict:
    p = Path(path or POLICY_FILE)
    key = str(p)
    if key not in _CACHE:
        import yaml
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for d in doc.get("defaults") or []:
            d["keys"] = [_fix_key(k) for k in d.get("keys") or []]
        _CACHE[key] = doc
    return _CACHE[key]


def _fix_key(k) -> str:
    return str(k)


def _norm_keys(obj):
    """A YAML 1.1 reader turns the bare key `on` into True; a MapSpec read without the plugin's loader is read as written here."""
    if isinstance(obj, dict):
        return {("on" if k is True else "off" if k is False else k): _norm_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_norm_keys(v) for v in obj]
    return obj


def lookup(spec: dict, path: str):
    cur = spec
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _same(v, default, same: list) -> bool:
    def n(x):
        if isinstance(x, str):
            s = x.strip().lower()
            return {"on": True, "yes": True, "true": True, "off": False, "no": False, "false": False, "none": "none"}.get(s, s)
        return x
    a = n(v)
    return a == n(default) or any(a == n(s) for s in same or [])


def asks_of(spec: dict) -> dict:
    """{key or default id: words} from the MapSpec's asks: a list of {set, words} (or a mapping key -> words)."""
    a = spec.get("asks")
    out: dict = {}
    if isinstance(a, dict):
        out = {str(k): str(v) for k, v in a.items()}
    elif isinstance(a, list):
        for it in a:
            if isinstance(it, dict) and it.get("set"):
                for k in (it["set"] if isinstance(it["set"], list) else [it["set"]]):
                    out[str(k)] = str(it.get("words") or "")
    return out


def _asked(d: dict, key: str, asks: dict) -> str | None:
    for k in (key, d["id"], *d["keys"], key.rsplit(".", 1)[0], key.split(".")[0]):
        if k in asks and asks[k].strip():
            return asks[k]
    return None


def intent_sets(spec: dict) -> dict:
    """{key: value} a named intent sets (data/intents/<name>.yaml, 5.0.0); empty when the MapSpec names none or the file is not there."""
    return dict(intent_doc((spec or {}).get("intent")).get("sets") or {})


def deviations(spec: dict, policy: dict | None = None) -> dict:
    """The MapSpec against the defaults. -> {items: [{id, key, value, default, box, words, says}], counts, line, unexplained_lines}."""
    pol = policy or load()
    spec = _norm_keys(spec or {})
    asks = asks_of(spec)
    isets = intent_sets(spec)
    items = []
    for d in pol.get("defaults") or []:
        for key in d["keys"]:
            v = lookup(spec, key)
            if v is _MISSING or _same(v, d.get("value"), d.get("same") or []):
                continue
            words = _asked(d, key, asks)
            if words is not None:
                box = "asked"
            elif key in isets and _same(v, isets[key], []):
                box = "intent"
            else:
                box = "unexplained"
            items.append({"id": d["id"], "key": key, "value": v, "default": d.get("value"), "box": box, "words": words, "says": d.get("says")})
            break
    free = sorted({f for f in pol.get("free") or [] if _sets_free(spec, f)})
    counts = {b: sum(1 for i in items if i["box"] == b) for b in ("asked", "intent", "unexplained")}
    counts["free"] = len(free)
    total = len(items)
    line = (f"deviations from the defaults: {total} (asked {counts['asked']}, intent {counts['intent']}, unexplained {counts['unexplained']}); "
            f"free choices: {counts['free']}")
    ul = [f"{i['key']}: {_show(i['value'])} was not asked for (the default is {_show(i['default'])}: {i['says']})" for i in items if i["box"] == "unexplained"]
    return {"policy_version": pol.get("policy_version"), "items": items, "free": free, "counts": counts, "line": line, "unexplained_lines": ul,
            "mode": mode()}


# ---------------------------------------------------------------------------------------------------------------- 5.0.0: the road enforces
MODES = ("report", "enforce")


def default_of(key: str, policy: dict | None = None):
    """The standing default's value for one MapSpec key (elements.logo.position -> lower-left), or None when no default names the key.
    One place for every drawing surface to ask (a map, a sheet, a page), so a default cannot differ between them."""
    for d in (policy or load()).get("defaults") or []:
        if key in (d.get("keys") or []):
            return d.get("value")
    return None


def mode() -> str:
    """How the build treats an unexplained deviation. `enforce` (a build on the road: gmap sets GMAP_POLICY=enforce, and `policy: enforce` in
    ~/.geo-mapping/config.yaml makes it so everywhere): the default is drawn and the change is said in one line. `report` (a build run
    directly, a pipeline, an atlas page, a test): the table alone, as in 4.2.0."""
    import os
    v = (os.environ.get("GMAP_POLICY") or "").strip().lower()
    if v not in MODES:
        try:
            import yaml
            home = os.environ.get("GEO_MAPPING_HOME")
            cp = (Path(home) if home else Path.home() / ".geo-mapping") / "config.yaml"
            v = str((yaml.safe_load(cp.read_text(encoding="utf-8")) or {}).get("policy") or "").strip().lower() if cp.is_file() else ""
        except Exception:  # noqa: BLE001
            v = ""
    return v if v in MODES else "report"


def _unset(spec: dict, path: str) -> None:
    parts = path.split(".")
    cur = spec
    for part in parts[:-1]:
        cur = cur.get(part) if isinstance(cur, dict) else None
        if not isinstance(cur, dict):
            return
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)
        for alias, real in ((True, "on"), (False, "off")):        # a plain YAML reader's key
            if parts[-1] == real:
                cur.pop(alias, None)


def _set(spec: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = spec
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def apply_intent(spec: dict) -> list[str]:
    """Write a named intent's settings into a loaded MapSpec where the MapSpec itself says nothing (in memory; the file is not changed).
    -> the keys it set. The MapSpec's own value wins: it is then a deviation like any other."""
    done = []
    for key, value in intent_sets(spec).items():
        if lookup(spec, key) is _MISSING:
            _set(spec, key, value)
            done.append(key)
    return done


def enforce(spec: dict, policy: dict | None = None) -> list[dict]:
    """5.0.0 (decision D1): every unexplained deviation is put back to its default in the loaded MapSpec (in memory; the file is not
    changed). -> what was put back: [{key, value, default, says, line}]. A default the intent set, or one the person asked for, stays."""
    dev = deviations(spec, policy)
    isets = intent_sets(spec)
    out = []
    for i in dev["items"]:
        if i["box"] != "unexplained":
            continue
        if i["key"] in isets:
            _set(spec, i["key"], isets[i["key"]])
            back = isets[i["key"]]
        else:
            _unset(spec, i["key"])
            back = i["default"]
        if i["key"].startswith("elements.") and i["key"].endswith(".on"):       # an element block needs its `on`
            _set(spec, i["key"], True if back in (True, "auto") else back)
        out.append({"key": i["key"], "value": i["value"], "default": back, "says": i["says"],
                    "line": f"{i['key']}: {_show(i['value'])} was not asked for; drawn as {_show(back)} ({i['says']})"})
    return out


def applicability(spec: dict) -> dict:
    """What does not apply to this map (data/applicability.yaml by `outcome`, plus the intent's own `checks_off`).
    -> {outcome, intent, checks_off: set, readings_off: set}."""
    import yaml
    spec = spec or {}
    out = {"outcome": spec.get("outcome"), "intent": spec.get("intent"), "checks_off": set(), "readings_off": set()}
    try:
        doc = yaml.safe_load((HERE.parent / "data" / "applicability.yaml").read_text(encoding="utf-8")) or {}
    except OSError:
        doc = {}
    row = (doc.get("outcomes") or {}).get(str(spec.get("outcome") or "")) or {}
    out["checks_off"] |= set(row.get("checks_off") or [])
    out["readings_off"] |= set(row.get("readings_off") or [])
    idoc = intent_doc(spec.get("intent"))
    out["checks_off"] |= set(idoc.get("checks_off") or [])
    if not out["outcome"] and idoc.get("outcome"):
        out["outcome"] = idoc["outcome"]
        row = (doc.get("outcomes") or {}).get(str(idoc["outcome"])) or {}
        out["checks_off"] |= set(row.get("checks_off") or [])
        out["readings_off"] |= set(row.get("readings_off") or [])
    return out


def intents() -> list[dict]:
    import yaml
    d = HERE.parent / "data" / "intents"
    return [yaml.safe_load(p.read_text(encoding="utf-8")) or {} for p in sorted(d.glob("*.yaml"))] if d.is_dir() else []


def intent_doc(name) -> dict:
    if not isinstance(name, str) or not name:
        return {}
    p = HERE.parent / "data" / "intents" / f"{name}.yaml"
    if not p.is_file():
        return {}
    import yaml
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _sets_free(spec: dict, f: str) -> bool:
    if lookup(spec, f) is not _MISSING:
        return True
    head = f.split(".")[0]
    if head in ("classification", "symbol", "palette"):
        tail = f.split(".", 1)[1] if "." in f else None
        for ly in spec.get("layers") or []:
            if isinstance(ly, dict) and head in ly and (tail is None or (isinstance(ly[head], dict) and tail in ly[head])):
                return True
    return False


def _show(v) -> str:
    if isinstance(v, bool):
        return "on" if v else "off"
    return str(v)


def digest(policy: dict | None = None) -> list[str]:
    return [str(x) for x in ((policy or load()).get("digest") or [])][:12]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("op", choices=["show", "digest", "check", "intents"])
    ap.add_argument("spec", nargs="?")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    pol = load()
    if a.op == "digest":
        print("\n".join(digest(pol)))
        return 0
    if a.op == "intents":
        for d in intents():
            print(f"{d.get('intent'):<11} for: {d.get('outcome'):<9} {d.get('says')}")
        return 0
    if a.op == "show":
        if a.json:
            print(json.dumps(pol, indent=2, default=str))
            return 0
        print("Fixed (never changed):")
        for r in pol.get("fixed") or []:
            print(f"  {r['id']}: {r['says']}")
        print("Defaults (changed only when the person asks):")
        for d in pol.get("defaults") or []:
            print(f"  {d['keys'][0]} = {_show(d.get('value'))}: {d['says']}")
        print("Free (the map maker's call): " + ", ".join(pol.get("free") or []))
        return 0
    if not a.spec:
        print("check needs a MapSpec file", file=sys.stderr)
        return 2
    import gmap_common as gc
    try:
        spec = gc.load_mapspec(Path(a.spec))
    except Exception as ex:  # noqa: BLE001
        print(f"policy: {a.spec} cannot be read: {str(ex).splitlines()[0][:200]}", file=sys.stderr)
        return 2
    dev = deviations(spec, pol)
    if a.json:
        print(json.dumps(dev, indent=2, default=str))
        return 0
    print(dev["line"])
    for i in dev["items"]:
        print(f"  {i['box']:<11} {i['key']} = {_show(i['value'])}" + (f"  (\"{i['words']}\")" if i["words"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
