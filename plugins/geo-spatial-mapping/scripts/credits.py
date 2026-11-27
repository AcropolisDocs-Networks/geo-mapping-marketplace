#!/usr/bin/env python3
"""credits: one consolidated "Sources:" line for the map footer (0.19.0).

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
    layers = [l for l in pv.all_layers(plan) if pv.credited(l)]      # 2.1.0: provenance.credit: false is the map maker's own annotation
    out = [layer_item(l, "data") for l in layers if l.role != "context"]
    out += [basemap_item(b) for b in plan.basemap if b.get("kind") != "hillshade"]
    out += [basemap_item(b) for b in getattr(plan, "detail_basemap", None) or []]       # 5.5.0 (D26-11): the detail inset's own ground
    out += [basemap_item(b) for b in plan.basemap if b.get("kind") == "hillshade"]
    # 2.5.0 (T-03, T-05): a relief mosaicked from several DEMs credits every source the window used
    out += [basemap_item({"entry": e, "kind": "hillshade"}) for b in plan.basemap if b.get("kind") == "hillshade" for e in b.get("extra_entries") or []]
    out += [layer_item(l, "context") for l in layers if l.role == "context"]
    out += [basemap_item({"entry": e, "kind": "names"}) for e in getattr(plan, "_name_entries", None) or []]     # 3.1.0: GeoNames towns a message named
    return [i for i in out if i]


_FILE_NAME = re.compile(r"\.(pmtiles|mbtiles|gpkg|geojson|parquet|shp|zip|tif|tiff|csv|json|fgb)\b", re.I)


def _norm_credit(x: str) -> str:
    return re.sub(r"\s+", " ", str(x).lower()).strip(" .;")


def _same_party(a: str, b: str) -> bool:
    n = lambda x: re.sub(r"[^a-z0-9]+", " ", str(x).lower().replace("©", "")).strip()
    return bool(n(a)) and (n(a) == n(b) or n(a) in n(b) or n(b) in n(a))


def _display(org: str, pub: dict | None, products: list, required: list, vias: list) -> str:
    # 3.4.0 (Day 21 #18): "derived by © OpenStreetMap contributors" under © OpenStreetMap contributors says the source twice: a via whose maker
    # is the credited organization itself is dropped
    vias = [v for v in vias if not any(_same_party(v.split(" by ", 1)[-1], x) for x in [org, *required])]
    head = "; ".join(required) if required else org
    inner, seen = [], set()
    for x in products + vias:
        k = _norm_credit(x)
        # 5.5.0 (D26-21): a product said once per organization, and not again when the head already names it ("GEBCO 2026 Grid (GEBCO 2026 Grid)")
        said = re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", _norm_credit(head)) is not None
        if k and k not in seen and not (said and (required or k == _norm_credit(org))):
            seen.add(k)
            inner.append(x)
    if not inner:
        return head
    if head.endswith(")"):           # 4.1.0: required wording stays word for word (MAP-LIC-017); "Census (ACS) (Boundary Files)" is not read twice
        return f"{head}, {', '.join(inner)}"
    if pub and pub.get("inline") and len(inner) == 1 and not required:
        return f"{head} {inner[0]}"
    return f"{head} ({', '.join(inner)})"


def consolidated(plan, label: str = "Sources") -> tuple[str, dict]:
    """(the one footer line, a report): each organization once, its products together, required wording as written (spec 3.2, 3.3)."""
    import provenance as pv
    groups: dict[str, dict] = {}
    for it in items(plan):
        # 3.7.0 (B16, Day 23 issue 16): two layers whose required wording is the same ("NYC Open Data") are one credit, and a layer whose wording
        # replaces its own name keeps that name as a product: "NYC Open Data (Restaurant Inspections, Parks Properties)", where the second
        # layer used to drop out of the line
        replaced = bool(it.required) and not _same_party(it.org, it.required)
        key = (it.required if replaced else it.org).lower()
        lab = it.required if replaced else it.org
        if key not in groups:
            # 4.1.0 (Day 24, D24-11): a credit whose wording names an organization already on the line joins it ("© OpenStreetMap
            # contributors (ODbL), Protomaps" for the ground and "© OpenStreetMap contributors" for three data layers were three credits)
            for k, g0 in groups.items():
                if any(_same_party(x, lab) for x in g0["labels"]):
                    key = k
                    break
        g = groups.setdefault(key, {"org": it.org, "pub": it.pub, "products": [], "required": [], "vias": [], "kinds": [], "labels": []})
        if lab not in g["labels"]:
            g["labels"].append(lab)
        # a layer whose wording replaces its own name keeps that name as a product, unless the name is another known publisher's (the
        # Day 24 cells, credited to the Census, printed "(© OpenStreetMap contributors)" inside the Census credit)
        own = ([it.org] if replaced and not it.products and it.pub is None else []) + list(it.products)
        own = [x for x in own if not _FILE_NAME.search(str(x))]          # a file name ("phoenix-metro-z13.pmtiles") is not a product
        g["products"] += [p for p in own if p not in g["products"]]
        if it.required and it.required not in g["required"]:
            g["required"].append(it.required)
        if it.via and it.via not in g["vias"]:
            g["vias"].append(it.via)
        g["kinds"].append(it.kind)
    for g in groups.values():                        # the shorter of two wordings one contains is said once, in the longer
        g["required"] = [r for r in g["required"] if not any(r != q and _norm_credit(r) in _norm_credit(q) for q in g["required"])]
    parts = [_display(g["org"], g["pub"], g["products"], g["required"], g["vias"]) for g in groups.values()]
    kept: list[str] = []
    for p in parts:                                  # a credit that another (required) credit already contains word for word is printed once
        if not any(p != q and p.lower() in q.lower() for q in parts) and p not in kept:
            kept.append(p)
    body = "; ".join(kept)
    appended = []
    norm = lambda s: re.sub(r"\s+", " ", s.strip().rstrip(".").lower())
    for req in pv.required_credit_lines(plan):
        if norm(req).rstrip(")") not in norm(body):            # 4.1.0: "Census (ACS, LODES)" is already said in "Census (ACS, LODES, CBF)"
            appended.append(req.rstrip("."))
            body = (body + "; " if body else "") + req.rstrip(".")
    for n in pv.api_notice_lines(plan) + pv.as_of_notes(plan) + pv.method_notes(plan):     # 1.3.0: an API notice and an as-of date; 3.4.0: how a modelled measure was made
        if norm(n) not in norm(body):
            body = (body + ". " if body else "") + n.rstrip(".")
    line = (f"{label}: " if label else "") + body + "."
    return line, {"organizations": [g["org"] for g in groups.values()], "printed": kept, "appended_required": appended}
