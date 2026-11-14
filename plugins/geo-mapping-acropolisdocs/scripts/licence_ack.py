#!/usr/bin/env python3
"""licence_ack: the person's own acknowledgment that they hold a third party's licence for data the plugin cannot license itself (1.15.0).

Some data can only be used under a licence the person buys or signs with its publisher (TeleGeography's submarine cable data is the first).
The plugin cannot read that licence, so it does not decide for the person and it does not hide the source either. The library entry says so
in `license.acknowledgment`:

    license:
      class: licensed
      acknowledgment:
        provider: TeleGeography
        product: Submarine Cable Map geocoded data
        terms_url: https://www2.telegeography.com/...
        uses: [internal, publication, commercial, redistribution]      # the uses the person can say their licence covers

The first time a map uses the entry, the person is shown a pop-up (the plugin server's `licence_acknowledge` tool, an MCP form; where the
client has no forms, the session asks the same question with AskUserQuestion). It asks whether they hold the provider's licence and which
uses it covers. Their answer is stored in their own folder, `.geo-mapping/licences/<entry id>.yaml`, with the date, the uses and (if they give
one) a licence reference and an end date; it is not a copy of the licence and holds no secret. From then on the entry is treated as the
person's licence allows: `publication` lets the map go to an external audience, `commercial` and `redistribution` likewise; `internal` alone
keeps it to internal maps. Past `valid_until` the acknowledgment lapses and the pop-up comes back. `withdraw` takes it back.

    licence_ack.py status [ENTRY] [--json]          what is acknowledged, for which uses, until when
    licence_ack.py ask ENTRY [--json]               the pop-up's text and choices
    licence_ack.py acknowledge ENTRY --uses internal,publication [--licence-ref TEXT] [--valid-until YYYY-MM-DD] --yes
                                                    record the answer; run only after the person answered the pop-up or the question
    licence_ack.py withdraw ENTRY

The acknowledgment is the person's statement about their own licence. The plugin keeps its other rules: the data is never bundled or fetched
for them, the provider's credit is printed, and the privacy gate and caps stay on.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

FOLDER = "licences"
USES = {
    "internal": "internal analysis and internal maps",
    "publication": "maps shown outside my organisation (clients, the web, print, social posts)",
    "commercial": "commercial deliverables (work sold or billed)",
    "redistribution": "passing the data itself on to others",
}
# what each use opens in the entry's licence block
FLAGS = {"publication": "external_ok", "commercial": "commercial_ok", "redistribution": "redistribute_ok"}
HOWS = ("pop-up", "asked")
_ID = re.compile(r"^[a-z0-9][a-z0-9.\-]{2,160}$")


class AckError(ValueError):
    pass


def required(entry: dict | None) -> dict | None:
    """The entry's acknowledgment block, or None when its data needs none."""
    if not entry:
        return None
    a = (entry.get("license") or {}).get("acknowledgment")
    return a if isinstance(a, dict) and a.get("provider") else None


def _dirs() -> list[Path]:
    out = [gc.workdir() / FOLDER, gc.outputs_dir() / FOLDER]
    if not os.environ.get("GEO_MAPPING_HOME"):
        out.append(Path.home() / gc.WORKDIR_NAME / FOLDER)
    seen, uniq = set(), []
    for p in out:
        k = str(p.resolve()) if p.exists() else str(p)
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def _name(entry_id: str) -> str:
    if not _ID.match(entry_id or ""):
        raise AckError(f"'{entry_id}' is not a library entry id")
    return f"{entry_id}.yaml"


def _today() -> dt.date:
    fixed = os.environ.get("GMAP_FIXED_DATE")
    return dt.date.fromisoformat(fixed) if fixed else dt.date.today()


def _read(p: Path) -> dict | None:
    import yaml
    try:
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable file is not an acknowledgment
        return None
    return d if isinstance(d, dict) and d.get("acknowledged") is True else None


def current(entry_id: str) -> dict | None:
    """The person's acknowledgment for the entry, or None (none made, withdrawn, or past its valid_until)."""
    for d in _dirs():
        p = d / _name(entry_id)
        if p.is_file():
            a = _read(p)
            if a is None:
                continue
            vu = str(a.get("valid_until") or "")
            if vu:
                try:
                    if dt.date.fromisoformat(vu) < _today():
                        continue
                except ValueError:
                    continue
            return {**a, "stored_in": str(p)}
    return None


def effective(lic: dict, ack: dict | None) -> dict:
    """The entry's licence block as the person's acknowledged licence allows: each acknowledged use turns its flag on."""
    out = dict(lic or {})
    if not ack:
        return out
    for u in ack.get("uses") or []:
        if u in FLAGS:
            out[FLAGS[u]] = True
    out["acknowledged"] = {k: ack.get(k) for k in ("acknowledged_on", "uses", "provider", "how", "valid_until") if ack.get(k)}
    return out


def uses_needed(audience: str, *, commercial: bool = False, redistribute: bool = False) -> list[str]:
    need = ["internal"]
    if audience in ("external", "regulatory"):
        need.append("publication")
    if commercial:
        need.append("commercial")
    if redistribute:
        need.append("redistribution")
    return need


def question(entry: dict) -> dict:
    """What the pop-up says: the message, the provider's terms address and the choices (one yes/no per use the entry offers)."""
    a = required(entry)
    if not a:
        raise AckError(f"library entry '{entry.get('id')}' needs no licence acknowledgment")
    offered = [u for u in (a.get("uses") or list(USES)) if u in USES]
    prod = a.get("product") or entry.get("title") or entry.get("id")
    msg = (a.get("question") or
           f"{prod} is licensed by {a['provider']}. The map plugin cannot read your licence, so it asks you: do you hold a licence from "
           f"{a['provider']} for this data, and which uses does it cover? Your answer is kept in your own folder and you can withdraw it.")
    return {"entry": entry.get("id"), "provider": a["provider"], "product": prod, "terms_url": a.get("terms_url") or (entry.get("source") or {}).get("url"),
            "message": msg, "uses": {u: USES[u] for u in offered},
            "ask_user_question": {
                "question": f"Do you hold a {a['provider']} licence for {prod}? Pick every use it covers.",
                "header": "Licence", "multiSelect": True,
                "options": [{"label": u.capitalize(), "description": USES[u]} for u in offered][:4]}}


def acknowledge(entry: dict, uses: list[str], *, how: str = "pop-up", licence_ref: str = "", valid_until: str = "", where: Path | None = None) -> dict:
    """Write the person's answer. Called only after they answered the pop-up (or the same question asked in the conversation)."""
    import yaml
    a = required(entry)
    if not a:
        raise AckError(f"library entry '{entry.get('id')}' needs no licence acknowledgment")
    offered = [u for u in (a.get("uses") or list(USES)) if u in USES]
    uses = [u.strip().lower() for u in uses if u and u.strip()]
    bad = [u for u in uses if u not in offered]
    if bad:
        raise AckError(f"{', '.join(bad)}: not a use this entry offers ({', '.join(offered)})")
    if not uses:
        raise AckError("no use was chosen: nothing is recorded (the person said they hold no licence, or did not answer)")
    if "internal" not in uses:
        uses = ["internal"] + uses                          # a licence for publication covers the internal map it starts as
    if how not in HOWS:
        raise AckError(f"how must be one of {', '.join(HOWS)}")
    if valid_until:
        try:
            if dt.date.fromisoformat(valid_until) < _today():
                raise AckError(f"valid_until {valid_until} is already past")
        except ValueError as ex:
            raise AckError(f"valid_until must be a date (YYYY-MM-DD): {ex}") from ex
    ref = re.sub(r"[\r\n]+", " ", str(licence_ref or ""))[:120]
    folder = where or (gc.outputs_dir(create=True) / FOLDER)
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / _name(entry["id"])
    rec = {"acknowledged": True, "entry": entry["id"], "provider": a["provider"], "product": a.get("product") or entry.get("title"),
           "terms_url": a.get("terms_url") or (entry.get("source") or {}).get("url"), "uses": uses, "acknowledged_on": _today().isoformat(),
           "how": how}
    if ref:
        rec["licence_ref"] = ref
    if valid_until:
        rec["valid_until"] = valid_until
    p.write_text("# The person's own statement that they hold this provider's licence for these uses (licence_ack.py, 1.15.0).\n"
                 + yaml.safe_dump(rec, sort_keys=False, width=200), encoding="utf-8")
    return {**rec, "stored_in": str(p)}


def withdraw(entry_id: str) -> dict:
    gone = []
    for d in _dirs():
        p = d / _name(entry_id)
        if p.is_file():
            p.unlink()
            gone.append(str(p))
    return {"withdrawn": gone, "acknowledged": current(entry_id) is not None}


def status(entry_id: str | None = None) -> list[dict]:
    out, seen = [], set()
    for d in _dirs():
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.yaml")):
            eid = p.stem
            if eid in seen or (entry_id and eid != entry_id):
                continue
            seen.add(eid)
            a = current(eid)
            out.append({"entry": eid, "acknowledged": a is not None, **({k: a.get(k) for k in ("provider", "uses", "acknowledged_on", "valid_until", "how", "stored_in")} if a else {})})
    return out


def refusal(entry: dict, audience: str = "internal") -> str:
    a = required(entry) or {}
    return (f"{entry.get('title') or entry.get('id')} is licensed by {a.get('provider', 'its publisher')}: before it is drawn, the person answers the "
            f"licence pop-up once (the plugin server's licence_acknowledge tool, entry_id={entry.get('id')}; or licence_ack.py ask). "
            f"This map needs: {', '.join(uses_needed(audience))}.")


def _entry(entry_id: str) -> dict:
    import ref_library as rl
    e = next((x for x in rl.all_entries("all") if x.get("id") == entry_id), None)
    if e is None:
        raise AckError(f"'{entry_id}' is not in the reference library")
    return e


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status"); s.add_argument("entry", nargs="?"); s.add_argument("--json", action="store_true")
    q = sub.add_parser("ask"); q.add_argument("entry"); q.add_argument("--json", action="store_true")
    a_ = sub.add_parser("acknowledge"); a_.add_argument("entry"); a_.add_argument("--uses", required=True)
    a_.add_argument("--licence-ref", default=""); a_.add_argument("--valid-until", default=""); a_.add_argument("--yes", action="store_true")
    w = sub.add_parser("withdraw"); w.add_argument("entry")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            print(json.dumps(status(a.entry), indent=2))
        elif a.cmd == "ask":
            qq = question(_entry(a.entry))
            if a.json:
                print(json.dumps(qq, indent=2))
            else:
                print(qq["message"] + "\nTerms: " + str(qq["terms_url"]) + "\nUses: " + "; ".join(f"{k}: {v}" for k, v in qq["uses"].items()))
        elif a.cmd == "acknowledge":
            if not a.yes:
                print("Record it only after the person answered the licence question; then add --yes.", file=sys.stderr)
                return gc.EXIT_USAGE
            print(json.dumps(acknowledge(_entry(a.entry), a.uses.split(","), how="asked", licence_ref=a.licence_ref, valid_until=a.valid_until), indent=2))
        else:
            print(json.dumps(withdraw(a.entry), indent=2))
    except AckError as ex:
        print(f"MAP-LIC-018: {ex}", file=sys.stderr)
        return gc.EXIT_USAGE
    return 0


if __name__ == "__main__":
    sys.exit(main())
