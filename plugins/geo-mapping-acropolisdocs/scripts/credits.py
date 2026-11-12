#!/usr/bin/env python3
"""credits: one consolidated "Sources:" line for the map footer (0.19.0; project spec spec-consolidated-source-credits-2026-11-07).

    elements:
      source_note:
        style: consolidated     # one line, each organization once (default on slide and social templates)
        style: detailed         # the per-kind lines of 0.18 (default on print templates): Source / attribution / Basemap / relief / Context
        label: Sources          # the consolidated line's lead word

How the line is built (spec 3.2):
  1. A credit item is taken from every drawn data layer, every basemap piece (ground, tiles), the relief, and every context layer (a context
     layer is credited like a data layer: its source or its library publisher, not only its attribution; the Overture gap of Day 7).
  2. Each item is filed under an organization: a layer's provenance.publisher ({name, product} or a name), else its library entry's
     source.publisher, matched against library/publishers.yaml (entry id globs, then substrings of the publisher name or the source text). An
     item no publisher matches stands under its own name.
  3. Organizations keep first-seen order (data, basemap, relief, context); the products used from each go in one parenthetical, once each.
     Vintages and release ids stay in the provenance JSON unless the layer sets provenance.show_vintage: true.
  4. Wording a licence requires (a layer's attribution under a licence that is not public domain or CC0, a library entry's attribution_text
     under such a licence) is printed as written: it replaces the organization's name, and any parenthetical follows it unchanged.
  5. provenance.derived_from: <publisher key> files a layer under the organization whose data it was made from, its own service in the
     parenthetical: "© OpenStreetMap contributors (walk times by Valhalla, FOSSGIS)".
  6. Public-domain courtesy text ("3DEP data courtesy of ...") is not required wording: the organization and product are printed instead.
  7. After the line is built, every credit `provenance.required_credit_lines` lists must appear in it; one that does not is appended and
     MAP-LIC-014 (WARN) says so. The per-layer record (sources, vintages, licences, hashes) is unchanged in the provenance JSON.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

NO_CREDIT_NEEDED = {"public-domain", "cc0", "public domain"}
_SCALE = re.compile(r",?\s*\b1?\d{1,3}\s?m\b.*$|,?\s*\b1:[\d,]+.*$", re.I)       # "Roads, 10m" and "1:500,000" are not products


@dataclass
class Item:
    org: str
    products: list = field(default_factory=list)
    required: str | None = None
    via: str | None = None                      # "walk times by Valhalla, FOSSGIS" for a derived layer
    pub: dict | None = None
    kind: str = "data"


_PUBS: list | None = None


def publishers() -> list[dict]:
    global _PUBS
    if _PUBS is None:
        import gmap_common as gc
        p = gc.plugin_root() / "library" / "publishers.yaml"
        _PUBS = list((gc.load_yaml(p) or {}).get("publishers") or []) if p.is_file() else []
    return _PUBS


def match_publisher(text: str, entry_id: str | None = None) -> dict | None:
    t = (text or "").lower()
    for pub in publishers():
        if entry_id and any(fnmatch.fnmatch(entry_id, g) for g in pub.get("ids") or []):
            return pub
    for pub in publishers():
        if any(m.lower() in t for m in pub.get("match") or []):
            return pub
    return None


def by_key(key: str) -> dict | None:
    return next((p for p in publishers() if p.get("key") == key or p.get("name") == key), None)


def product_of(pub: dict | None, *texts) -> str | None:
    if not pub or pub.get("products") == "none":
        return None
    blob = " ".join(str(x) for x in texts if x)
    for rule in pub.get("products") or []:
        m = str(rule.get("match", ""))
        if m and re.search(r"(?<![A-Za-z0-9])" + re.escape(m) + r"(?![A-Za-z0-9])", blob, re.IGNORECASE):     # 1.2.0: whole words
            return rule.get("product")
    return None


def _clean_product(p) -> str | None:
    if not p:
        return None
    s = _SCALE.sub("", str(p)).strip(" ,;.")
    return s or None


def _required(lic: str, text) -> str | None:
    if not text or str(lic or "").strip().lower() in NO_CREDIT_NEEDED:
        return None
    return str(text).strip().rstrip(".")


def layer_item(l, kind: str) -> Item | None:
    import provenance as pv
    p = l.provenance or {}
    entry = l.entry or {}
    esrc = entry.get("source") or {}
    pub_decl = p.get("publisher")
    if isinstance(pub_decl, str):
        pub_decl = {"name": pub_decl}
    source = pv.withheld(p.get("source")) if p.get("source") else None
    name = (pub_decl or {}).get("name") or esrc.get("publisher") or source
    if not name:
        return None
    pub = match_publisher(str(name), entry.get("id")) or match_publisher(str(source or ""), entry.get("id"))
    prod = (pub_decl or {}).get("product") or product_of(pub, source, esrc.get("product"), (pub_decl or {}).get("name"))
    if prod is None and pub is None:
        prod = _clean_product(esrc.get("product"))
    if prod and p.get("show_vintage") and p.get("vintage"):
        prod = f"{prod} {p['vintage']}"
    lic = p.get("license") or (entry.get("license") or {}).get("class")
    req = _required(lic, p.get("attribution")) or _required((entry.get("license") or {}).get("class"), (entry.get("license") or {}).get("attribution_text"))
    it = Item(org=pub["name"] if pub else str(name), products=[prod] if prod else [], required=req, pub=pub, kind=kind)
    if p.get("derived_from"):                       # made from another organization's data: filed under that organization (spec 3.2.6)
        base = by_key(str(p["derived_from"]))
        if base:
            what = (pub_decl or {}).get("product") or p.get("derived_what") or "derived"
            it.via = f"{what} by {(pub_decl or {}).get('name') or it.org}"
            it.org, it.pub, it.products = base["name"], base, []
            if it.required and base["name"].lower().strip("© ") in it.required.lower():
                pass
    return it


def basemap_item(b: dict) -> Item | None:
    e = b.get("entry") or {}
    src = e.get("source") or {}
    lic = e.get("license") or {}
    name = src.get("publisher") or lic.get("attribution_text") or (b.get("record") or {}).get("source_name")
    if not name:
        return None
    pub = match_publisher(str(name), e.get("id")) or match_publisher(str(lic.get("attribution_text") or ""), e.get("id"))
    prod = product_of(pub, src.get("product"), lic.get("attribution_text"), name)
    if prod is None and pub is None:
        prod = _clean_product(src.get("product"))
    return Item(org=pub["name"] if pub else str(name).rstrip("."), products=[prod] if prod else [], pub=pub,
                required=_required(lic.get("class"), lic.get("attribution_text")), kind="relief" if b.get("kind") == "hillshade" else "basemap")


def items(plan) -> list[Item]:
    import provenance as pv
    # 1.2.0 (Day 9 issue 2): a draw: false helper is a source of the map's numbers, so it is credited too (the skill says so)
    layers = list(pv.all_layers(plan))
    out = [layer_item(l, "data") for l in layers if l.role != "context"]
    out += [basemap_item(b) for b in plan.basemap if b.get("kind") != "hillshade"]
    out += [basemap_item(b) for b in plan.basemap if b.get("kind") == "hillshade"]
    out += [layer_item(l, "context") for l in layers if l.role == "context"]
    return [i for i in out if i]


def _display(org: str, pub: dict | None, products: list, required: list, vias: list) -> str:
    inner = list(dict.fromkeys(products + vias))
    head = "; ".join(required) if required else org
    if not inner:
        return head
    if pub and pub.get("inline") and len(inner) == 1 and not required:
        return f"{head} {inner[0]}"
    return f"{head} ({', '.join(inner)})"


def consolidated(plan, label: str = "Sources") -> tuple[str, dict]:
    """(the one footer line, a report): each organization once, its products together, required wording as written (spec 3.2, 3.3)."""
    import provenance as pv
    groups: dict[str, dict] = {}
    for it in items(plan):
        g = groups.setdefault(it.org.lower(), {"org": it.org, "pub": it.pub, "products": [], "required": [], "vias": [], "kinds": []})
        g["products"] += [p for p in it.products if p not in g["products"]]
        if it.required and it.required not in g["required"]:
            g["required"].append(it.required)
        if it.via and it.via not in g["vias"]:
            g["vias"].append(it.via)
        g["kinds"].append(it.kind)
    parts = [_display(g["org"], g["pub"], g["products"], g["required"], g["vias"]) for g in groups.values()]
    kept: list[str] = []
    for p in parts:                                  # a credit that another (required) credit already contains word for word is printed once
        if not any(p != q and p.lower() in q.lower() for q in parts) and p not in kept:
            kept.append(p)
    body = "; ".join(kept)
    appended = []
    norm = lambda s: re.sub(r"\s+", " ", s.strip().rstrip(".").lower())
    for req in pv.required_credit_lines(plan):
        if norm(req) not in norm(body):
            appended.append(req.rstrip("."))
            body = (body + "; " if body else "") + req.rstrip(".")
    for n in pv.api_notice_lines(plan) + pv.as_of_notes(plan):     # 1.3.0: an API notice (Census "not endorsed or certified") and an as-of date, once each
        if norm(n) not in norm(body):
            body = (body + ". " if body else "") + n.rstrip(".")
    line = (f"{label}: " if label else "") + body + "."
    return line, {"organizations": [g["org"] for g in groups.values()], "printed": kept, "appended_required": appended}
