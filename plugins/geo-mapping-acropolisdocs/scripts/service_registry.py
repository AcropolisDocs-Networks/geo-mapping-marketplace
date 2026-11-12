#!/usr/bin/env python3
"""service_registry: stores and checks service profiles (spec 6.4, `geo-service-registry`).

A profile holds references and limits, never a secret. Profiles live in the working folder (`.geo-mapping/services/<name>.yaml`). A host is used
only after the person confirmed it (MAP-SVC-001): `save` needs the host name typed back as `--confirm-host`.

Shipped profiles (M13): the edition carries read-only profiles for well-known public services in `profiles/*.yaml` (TIGERweb, FCC, HIFLD,
USGS, NWS, FEMA NFHL, state portals). Each is `public: true`, `auth.method: none` (1.0.2: or a credential the library declares for that
host, as NASA FIRMS's two), route `mcp`, with its host confirmed by the maintainer
on review. They have the lowest precedence: a working-folder profile with the same name overrides a shipped one, `list` marks each
with `shipped`, and `delete` refuses a shipped profile (remove the override instead). A profile may name `none` (public, fetched by the bundled server), `user-managed`
(the person holds credentials and runs the request; capture only), `basic` (an approved run on the person's computer, with a reference to a secrets file; never a value)
or, from 0.6.0, `credential` with `credentials: cred:NAME` (a key registered with map-credentials; the MCP server on the person's computer reads it at request time
and sends it only to this profile's host; capture only).

    service_registry.py list | get NAME | validate FILE | save FILE --confirm-host HOST | delete NAME
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import sys
import urllib.parse
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

LIMIT_DEFAULTS = {"max_features_per_request": 1000, "max_concurrency": 1, "timeout_s": 30, "max_bytes": 50_000_000, "max_total_features": 200_000}


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def schema() -> dict:
    return json.loads((gc.plugin_root() / "schemas" / "service-profile.schema.json").read_text())


def services_dir(create: bool = False) -> Path:
    d = gc.workdir(create) / "services"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def host_of(profile: dict) -> str | None:
    return urllib.parse.urlsplit(profile.get("base_url", "")).hostname


def literal_private(host: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return (host or "").lower() in ("localhost",) or (host or "").lower().endswith((".local", ".internal", ".lan"))
    return ip.is_private or ip.is_loopback or ip.is_link_local


def basic_rules(profile: dict, net: dict, route: str | None) -> list[dict]:
    """HTTP Basic is allowed only as an approved run on the person's computer, by reference to a secrets file, over https, and never live (spec 6.4, M9)."""
    import credential_broker as cb
    out: list[dict] = []
    if net["route"] != "device-run" or net["public"]:
        out.append(finding("MAP-SVC-019", "BLOCK", "auth method basic is available only for network.route device-run with public false: the plugin runs the request on your computer and the bundled server never holds credentials"))
    if route in ("mcp", "live"):
        out.append(finding("MAP-SVC-017" if route == "live" else "MAP-SVC-019", "BLOCK",
                           "an authenticated profile is capture-only: a live layer cannot carry credentials, and the bundled server never uses them" if route == "live"
                           else "the bundled server serves only profiles with auth none, route mcp and public true"))
    if not profile["base_url"].lower().startswith("https://"):
        out.append(finding("MAP-SVC-016", "BLOCK", "credentials are sent only over https; this profile's address is not https"))
    refs = profile["auth"].get("credentials") or {}
    if isinstance(refs, str):
        out.append(finding("MAP-SVC-029", "BLOCK", "auth method basic takes credentials: {netrc: \"netrc:PATH\"} or {file: \"file:PATH\"}; a cred:NAME reference goes with method credential"))
        return out
    if not refs:
        out.append(finding("MAP-SVC-029", "BLOCK", "auth method basic needs a credential reference: credentials: {netrc: \"netrc:PATH\"} or {file: \"file:PATH\"}"))
    for k, v in refs.items():
        if k not in cb.KINDS or not v.startswith(k + ":"):
            out.append(finding("MAP-SVC-029", "BLOCK", f"credentials.{k}: only netrc: and file: references are accepted (never a user name, password or env: name)"))
            continue
        r, fs = cb.parse_ref(v)
        out += fs
    return out


def credential_rules(profile: dict, net: dict, route: str | None) -> list[dict]:
    """0.6.0: a profile that names a registered credential (cred:NAME). The MCP server on the person's computer reads the key at request time
    and sends it only to this profile's host over https; the profile holds the name, never a value. Capture-only (MAP-SVC-017, MAP-CRED-011)."""
    import credentials as cr
    out: list[dict] = []
    ref = profile["auth"].get("credentials")
    if not isinstance(ref, str):
        out.append(finding("MAP-CRED-001", "BLOCK", "auth method credential needs credentials: cred:NAME (a credential registered with map-credentials add NAME)"))
    else:
        r, fs = cr.parse_ref(ref)
        out += fs
        if r and r["kind"] != "cred":
            out.append(finding("MAP-CRED-001", "BLOCK", "a service profile names a registered credential, cred:NAME; never a path or a value"))
    if not profile["base_url"].lower().startswith("https://"):
        out.append(finding("MAP-CRED-005", "BLOCK", "a credential is sent only over https; this profile's address is not https"))
    if net["route"] != "mcp" or not net["public"]:
        out.append(finding("MAP-SVC-019", "BLOCK", "a profile with a registered credential is served by the MCP server on your computer: network.route mcp and public true"))
    if route == "live":
        out.append(finding("MAP-SVC-017", "BLOCK", "an authenticated profile is capture-only: a live layer cannot carry credentials"))
        out.append(finding("MAP-CRED-011", "BLOCK", "a credential is never written into a web map or other shared output"))
    return out


def credential_ref(profile: dict) -> str | None:
    """The cred:NAME a profile names, or None."""
    au = profile.get("auth") or {}
    ref = au.get("credentials")
    return ref if au.get("method") == "credential" and isinstance(ref, str) else None


def validate(profile: dict, *, route: str | None = None) -> list[dict]:
    """Schema, then the rules. `route` is how it will be used: 'mcp' (the bundled server), 'capture' or 'live'."""
    import jsonschema
    out: list[dict] = []
    for e in sorted(jsonschema.Draft202012Validator(schema()).iter_errors(profile), key=lambda e: list(e.path)):
        out.append(finding("MAP-SVC-001", "BLOCK", f"profile: {'.'.join(str(p) for p in e.path) or '(root)'}: {e.message}"))
    if out:
        return out
    method = profile["auth"]["method"]
    net0 = profile["network"]
    if method == "basic":
        out += basic_rules(profile, net0, route)
    elif method == "credential":
        out += credential_rules(profile, net0, route)
    elif method not in ("none", "user-managed"):
        out.append(finding("MAP-SVC-019", "BLOCK", f"auth method '{method}' is not available: use none (public), user-managed (you run the request), basic (an approved run with a credential reference) "
                                                  "or credential (a key registered with map-credentials)"))
    elif (profile["auth"].get("credentials")):
        out.append(finding("MAP-SVC-029", "BLOCK", "credential references belong to auth method basic or credential; this profile's method does not use them"))
    u = urllib.parse.urlsplit(profile["base_url"])
    if u.username or u.password:
        out.append(finding("MAP-SVC-022", "BLOCK", "the address carries a user name or password; remove it (credentials never go in a profile)"))
    if u.query and any(k.lower() in ("key", "token", "apikey", "api_key", "password") for k, _ in urllib.parse.parse_qsl(u.query)):
        out.append(finding("MAP-SVC-022", "BLOCK", "the address carries a key or token in its query; remove it"))
    if u.scheme == "http":
        out.append(finding("MAP-SVC-020", "WARN", "http: a page that loads this service over https would be blocked as mixed content; prefer https"))
    net = profile["network"]
    if method == "user-managed" and (net["route"] == "mcp" or net["public"]):
        out.append(finding("MAP-SVC-025", "BLOCK", "a user-managed profile is capture-only: network.route must be user-run and network.public false; the bundled server never fetches from it"))
    if method == "none" and net["route"] == "mcp" and not net["public"]:
        out.append(finding("MAP-SVC-020", "BLOCK", "the bundled server reaches public hosts only; set public: true for a public service, or use a user-run profile"))
    if route == "live" and method not in ("none", "basic", "credential"):
        out.append(finding("MAP-SVC-017", "BLOCK", "an authenticated or user-managed profile is capture-only; a live layer cannot carry credentials"))
    if route == "mcp":
        if net["route"] != "mcp" or method not in ("none", "credential") or not net["public"]:
            out.append(finding("MAP-SVC-019", "BLOCK", "the bundled server serves only profiles with route mcp and public true, with auth none or a registered credential (cred:NAME)"))
        if literal_private(host_of(profile)):
            out.append(finding("MAP-SVC-020", "BLOCK", "the host is a private or local address; the bundled server reaches public hosts only"))
        if not profile.get("host_confirmed_on"):
            out.append(finding("MAP-SVC-001", "BLOCK", "the host has not been confirmed: save the profile with the host name typed back (--confirm-host)"))
    lic = profile.get("license") or {}
    if not lic.get("default"):
        out.append(finding("MAP-SVC-012", "WARN", "no license is recorded for this service; set license.default, or the capture will say 'unknown' and block external publication"))
    return out


def with_defaults(profile: dict) -> dict:
    p = dict(profile)
    lim = dict(LIMIT_DEFAULTS)
    lim.update({k: v for k, v in (profile.get("limits") or {}).items() if v is not None})
    p["limits"] = lim
    return p


def path_of(name: str) -> Path:
    """The working-folder path of a profile (where `save` writes and `delete` removes)."""
    return services_dir() / f"{name}.yaml"


def shipped_dir() -> Path:
    """Read-only profiles that ship with the edition (`profiles/` beside `scripts/`); absent in a source tree without them."""
    return gc.plugin_root() / "profiles"


def shipped_path_of(name: str) -> Path:
    return shipped_dir() / f"{name}.yaml"


def is_shipped(name: str) -> bool:
    return shipped_path_of(name).is_file()


def _shipped_ok(y: dict) -> bool:
    """A shipped profile is public and served by the bundled server, unauthenticated or (1.0.2) naming a credential the library declares for the
    same host (NASA FIRMS: the person's own MAP_KEY, registered with map-credentials); anything else in `profiles/` is ignored."""
    net = y.get("network") or {}
    if net.get("public") is not True or net.get("route") != "mcp":
        return False
    method = (y.get("auth") or {}).get("method")
    return method == "none" or (method == "credential" and _shipped_credential_ok(y))


def _shipped_credential_ok(y: dict) -> bool:
    ref = credential_ref(y)
    if not ref or not ref.startswith("cred:") or not str(y.get("base_url") or "").lower().startswith("https://"):
        return False
    try:
        import credentials as cr
        b, _ = cr.library_defaults(ref[5:])
    except Exception:  # noqa: BLE001 - no library here
        return False
    return bool(b) and (b.get("host") or "").lower() == (host_of(y) or "").lower()


def load(name: str) -> dict:
    """The working-folder profile when there is one, else the shipped profile of that name."""
    p = path_of(name)
    if p.is_file():
        return with_defaults(gc.load_yaml(p))
    s = shipped_path_of(name)
    if s.is_file():
        y = gc.load_yaml(s)
        if _shipped_ok(y):
            return with_defaults(y)
    raise FileNotFoundError(f"no service profile '{name}' in {services_dir()} and none shipped under that name; save one first (service_registry.py save)")


def _row(y: dict, stem: str, shipped: bool, overrides: bool = False) -> dict:
    public = (y.get("host_visibility") != "withheld")
    row = {"profile": y.get("profile", stem), "type": y.get("type"), "host": host_of(y) if public else "internal data service", "auth": (y.get("auth") or {}).get("method"),
           "route": (y.get("network") or {}).get("route"), "public": (y.get("network") or {}).get("public"), "confirmed": bool(y.get("host_confirmed_on")), "verified_on": y.get("verified_on"),
           "shipped": shipped}
    if overrides:
        row["overrides_shipped"] = True
    return row


def list_profiles() -> list[dict]:
    """Working-folder profiles first (a working-folder profile hides the shipped one of the same name), then the shipped ones."""
    d = services_dir()
    rows, seen = [], set()
    for p in sorted(d.glob("*.yaml")) if d.is_dir() else []:
        try:
            y = gc.load_yaml(p)
        except Exception:  # noqa: BLE001
            continue
        seen.add(p.stem)
        rows.append(_row(y, p.stem, False, overrides=is_shipped(p.stem)))
    sd = shipped_dir()
    for p in sorted(sd.glob("*.yaml")) if sd.is_dir() else []:
        if p.stem in seen:
            continue
        try:
            y = gc.load_yaml(p)
        except Exception:  # noqa: BLE001
            continue
        if _shipped_ok(y):
            rows.append(_row(y, p.stem, True))
    return rows


def save(profile: dict, confirm_host: str | None) -> tuple[Path | None, list[dict]]:
    """Validate and store. The host must be typed back; nothing is stored on a BLOCK."""
    fs = validate({**profile, "host_confirmed_on": profile.get("host_confirmed_on") or "x"})
    h = host_of(profile)
    if not confirm_host or confirm_host.strip().lower() != (h or "").lower():
        fs.append(finding("MAP-SVC-001", "BLOCK", f"the host must be confirmed by the person: pass the host name ({h}) as confirm_host"))
    if any(f["severity"] == "BLOCK" for f in fs):
        return None, fs
    stored = {k: v for k, v in profile.items() if k != "host_confirmed_on"}
    stored["host_confirmed_on"] = dt.date.today().isoformat()
    p = path_of(profile["profile"])
    services_dir(True)
    p.write_text(yaml.safe_dump(stored, sort_keys=False))
    try:                                                     # 0.23.0: a service you add is a customization; say so in plain words, never block on it
        import customizations as cz
        ack = cz.status()["acknowledged"]
    except Exception:  # noqa: BLE001
        ack, cz = False, None
    fs.append(finding("MAP-SVC-001", "INFO", "A service you add is a customization beyond the plugin's defaults: keep your use within its terms"
                      + (", as your acknowledgment says" if ack else f"; make the one acknowledgment when you are ready ({cz.command() if cz else 'map-services'})")))
    return p, fs


def public_view(profile: dict) -> dict:
    """What may be shown: the host is withheld when the profile says so (MAP-LIC-009)."""
    p = dict(profile)
    name = p.get("profile")
    if name:
        p["shipped"] = bool(is_shipped(name) and not path_of(name).is_file())
    if p.get("host_visibility") == "withheld":
        for k in ("base_url", "endpoints", "workspace"):
            p.pop(k, None)
        p["host"] = "internal data service"
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="op", required=True)
    sub.add_parser("list")
    g = sub.add_parser("get"); g.add_argument("name")
    v = sub.add_parser("validate"); v.add_argument("file"); v.add_argument("--route", choices=["mcp", "capture"])
    s = sub.add_parser("save"); s.add_argument("file"); s.add_argument("--confirm-host")
    d = sub.add_parser("delete"); d.add_argument("name")
    a = ap.parse_args(argv)
    if a.op == "list":
        print(json.dumps(list_profiles(), indent=2))
        return gc.EXIT_OK
    if a.op == "get":
        try:
            print(json.dumps(public_view(load(a.name)), indent=2, default=str))
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            return gc.EXIT_USAGE
        return gc.EXIT_OK
    if a.op == "delete":
        p = path_of(a.name)
        if not p.is_file():
            if is_shipped(a.name):
                print(f"'{a.name}' is a shipped profile (read-only); save a profile of the same name in the working folder to override it", file=sys.stderr)
            else:
                print(f"no profile '{a.name}'", file=sys.stderr)
            return gc.EXIT_USAGE
        p.unlink()
        print(f"deleted {a.name}")
        return gc.EXIT_OK
    prof = gc.load_yaml(Path(a.file))
    if a.op == "validate":
        fs = validate(prof, route=a.route)
    else:
        path, fs = save(prof, a.confirm_host)
        if path:
            print(f"saved {path}")
    print(json.dumps(fs, indent=2))
    return gc.EXIT_LINT if any(f["severity"] == "BLOCK" for f in fs) else gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
