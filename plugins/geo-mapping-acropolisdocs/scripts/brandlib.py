"""Brand pack access. The plugin ships its default brand under brands/<slug>/; a brand a person adds (set_brand.py add / new) is a pack
folder outside the plugin (gmap_common.user_brand_roots). Nothing here reads another plugin."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import gmap_common as gc


class BrandError(Exception):
    """A user-facing, one-line brand problem (never a traceback)."""


def sha256_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def load_pack(slug: str) -> dict:
    d = gc.brand_dir(slug)
    if not (d / "PACK.json").is_file():
        raise BrandError(f"There is no brand '{slug}' here. {available_note()}")
    return {
        "slug": slug,
        "dir": d,
        "brand": json.loads((d / "brand.json").read_text()),
        "pack": json.loads((d / "PACK.json").read_text()),
        "tokens": gc.load_yaml(d / "map-tokens.yaml"),
    }


def available(manifest: dict | None = None) -> list[str]:
    """Shipped brands first, then the ones this person added (1.7.0)."""
    return gc.available_brands(manifest)


def origin(slug: str) -> str:
    """'shipped' for a brand the plugin carries, 'added' for a pack a person added, '' when there is no such brand."""
    d = gc.brand_dir(slug)
    if not (d / "PACK.json").is_file():
        return ""
    return "shipped" if d.parent == gc.plugin_root() / gc.BRANDS_DIRNAME else "added"


def brand_command() -> str:
    try:
        return f"/{gc.build_manifest()['plugin_name']}:brand"
    except Exception:  # noqa: BLE001 - a message helper never fails
        return "/geo-mapping-acropolisdocs:brand"


def available_note(manifest: dict | None = None) -> str:
    try:
        names = ", ".join(available(manifest))
    except Exception:  # noqa: BLE001
        names = ""
    return (f"Brands here: {names}. " if names else "") + f"Add your own with {brand_command()} (add a pack, or make one from a name, a colour and a logo)."


def brand_name(slug: str) -> str:
    return load_pack(slug)["brand"]["identity"]["name"]


def user_default(manifest: dict) -> tuple[str | None, str | None]:
    """The per-user default brand.

    Order: the file written by geo-set-brand, then the plugin user-config option exposed as
    CLAUDE_PLUGIN_OPTION_DEFAULT_BRAND. Whether the host exposes user config is decided by spike P5.
    """
    if manifest["brand_selection"] != "selectable":
        return None, None
    f = gc.workdir() / "user-defaults.yaml"
    if f.is_file():
        try:
            v = (gc.load_yaml(f) or {}).get("default_brand")
        except Exception:  # noqa: BLE001
            v = None
        if v:
            return str(v), "file"
    v = os.environ.get("CLAUDE_PLUGIN_OPTION_DEFAULT_BRAND")
    if v:
        return v, "user-config"
    return None, None


def resolve(manifest: dict, spec_brand: str | None = None, request_brand: str | None = None) -> dict:
    """Resolve which brand applies (spec 11.4a): the one named, else this person's default, else the plugin's. Never asks; never
    substitutes silently. 1.7.0: a named brand may be one the person added."""
    here = available(manifest)

    def need(slug: str) -> str:
        if slug in here:
            return slug
        raise BrandError(f"There is no brand '{slug}' here. {available_note(manifest)}")

    named = request_brand or spec_brand
    if named:
        return {"brand": need(named), "rule": "named", "source": "request" if request_brand else "mapspec"}
    default, src = user_default(manifest)
    if default:
        if default in here:
            return {"brand": default, "rule": "user_default", "source": src}
        # a stored default that is not here (its pack was removed, or lives in another project) is ignored, with a note, never an error
        return {"brand": manifest["default_content_brand"], "rule": "edition_default",
                "source": "manifest", "note": f"stored default '{default}' is not available here"}
    return {"brand": manifest["default_content_brand"], "rule": "edition_default", "source": "manifest"}


def house_accreditation(manifest: dict | None = None) -> dict:
    """The plugin's own hidden credit (file metadata only, never drawn): read from the shipped house pack."""
    m = manifest or gc.build_manifest()
    return json.loads((gc.plugin_root() / gc.BRANDS_DIRNAME / m["house_brand"] / "PACK.json").read_text())["accreditation"]


def logo_colors(brand: dict) -> set[str]:
    import re
    found: set[str] = set()
    logo = brand.get("logo", {})

    def walk(v):
        if isinstance(v, str) and "<svg" in v:
            found.update(re.findall(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b", v))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)

    walk(logo)
    return found


def inter_font_path() -> Path:
    return gc.plugin_root() / "assets" / "fonts" / "inter" / "inter-latin-wght-normal.woff2"


def inter_font_face_css() -> str:
    data = base64.b64encode(inter_font_path().read_bytes()).decode("ascii")
    return ("@font-face{font-family:'Inter';font-style:normal;font-weight:100 900;font-display:swap;"
            f"src:url(data:font/woff2;base64,{data}) format('woff2');}}")


def accreditation_props(manifest: dict, slug: str) -> dict:
    """Hidden accreditation carried in metadata only; never rendered (spec 11.7)."""
    pack = load_pack(slug)
    acc = pack["pack"].get("accreditation") or house_accreditation(manifest)
    brand_hash = pack["pack"]["files"]["brand.json"][:12]
    return {
        "house": acc.get("html_label", "AcropolisDocs"),
        "house_url": acc.get("href", "https://acropolisdocs.com"),
        "plugin": manifest["plugin_name"],
        "plugin_version": manifest.get("version"),
        "brand_pack": f"{slug}@{brand_hash}",
        "content_brand": brand_name(slug),
    }
