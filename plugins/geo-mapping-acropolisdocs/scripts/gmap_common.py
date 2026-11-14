"""Shared helpers for every script in this plugin.

Each script resolves its own plugin root from its own file location and never searches the
filesystem for other plugins (spec 11.4, guard 3). Nothing here writes outside the working
directory or the output directory (spec 12.9, MAP-ED-006).
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_LINT = 3
EXIT_REFUSED = 4
EXIT_SERVICE = 5
EXIT_BRAND_OUTSIDE_BUILD = 6

DOM_PREFIX = "gmap"
WORKDIR_NAME = ".geo-mapping"
FONT_FAMILY = "Inter"
EDITION_GLOB = "geo-mapping-*"


def plugin_root() -> Path:
    """The built edition's root: the parent of scripts/, resolved from this file."""
    return Path(__file__).resolve().parent.parent


def build_manifest() -> dict:
    p = plugin_root() / "BUILD-MANIFEST.json"
    if not p.is_file():
        raise FileNotFoundError("BUILD-MANIFEST.json is missing: this is not a built edition")
    return json.loads(p.read_text())


_POSIX_SYSTEM = ("/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/lib", "/etc", "/proc", "/sys", "/dev", "/boot", "/System")


def system_folder(p, *, nt: bool | None = None, env: dict | None = None) -> bool:
    """True for a folder no map work belongs in (0.9.4): the Windows folder and the program folders, a drive root, or the root and the system
    folders of macOS and Linux. The desktop app starts the MCP server in C:\\Windows\\System32, so a working folder beside the process would be there."""
    nt = (os.name == "nt") if nt is None else nt
    env = os.environ if env is None else env
    if nt:
        from pathlib import PureWindowsPath
        w = PureWindowsPath(str(p))
        if not w.drive or str(w) in (w.anchor, w.drive):
            return True
        roots = [env.get(k) for k in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")] + ["C:\\Windows"]
        low = [x.lower() for x in w.parts]
        for r in roots:
            if r:
                rp = [x.lower() for x in PureWindowsPath(r).parts]
                if rp and low[:len(rp)] == rp:
                    return True
        return False
    s = str(p).rstrip("/") or "/"
    return s == "/" or any(s == r or s.startswith(r + "/") for r in _POSIX_SYSTEM)


def default_workdir() -> Path:
    """.geo-mapping/ in the current folder, or the person's own ~/.geo-mapping/ (%USERPROFILE%\\.geo-mapping) when the process was started in a
    system folder or a folder it cannot write (0.9.4). A session started in the home folder lands in the same place either way."""
    try:
        cwd = Path.cwd()
    except OSError:                       # the folder was removed under the process
        return Path.home() / WORKDIR_NAME
    if system_folder(cwd) or not os.access(cwd, os.W_OK):
        return Path.home() / WORKDIR_NAME
    return cwd / WORKDIR_NAME


def workdir(create: bool = False) -> Path:
    """The one shared working directory, .geo-mapping/ (spec 11.5): beside the process, or in the home folder when the process runs in a system
    folder (default_workdir).

    GEO_MAPPING_HOME overrides it (used by tests and by the persistence spike, Appendix H P6).
    """
    base = os.environ.get("GEO_MAPPING_HOME")
    p = Path(base) if base else default_workdir()
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


_OUTPUTS: "contextvars.ContextVar[Path | None]" = contextvars.ContextVar("gmap_outputs", default=None)


def outputs_dir(create: bool = False) -> Path:
    """Where fetched data, captures and the library cache are written (0.9.4): FOLDER/.geo-mapping when a tool call named a folder (the MCP
    server's work_folder=, so a map built in the person's project folder finds what was fetched), else the working directory. Profiles, host
    confirmations and settings stay in workdir()."""
    p = _OUTPUTS.get() or workdir()
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


_SPEC_DIR: "contextvars.ContextVar[Path | None]" = contextvars.ContextVar("gmap_spec_dir", default=None)


def note_spec_folder(spec_path) -> None:
    """1.14.0 (Day 11 issue 5): the folder of the MapSpec being built, so a cache made for that map (`ground_detail.py prepare --spec`, which
    writes beside the MapSpec) is found wherever the build is started from."""
    try:
        _SPEC_DIR.set(Path(spec_path).resolve().parent / WORKDIR_NAME)
    except OSError:
        pass


def cache_roots() -> list[Path]:
    """The .geo-mapping folders a cache is READ from, in order (1.14.0): where this call writes (outputs_dir), the MapSpec's own folder, the
    working directory, and the person's home folder. Writing still goes to outputs_dir() only. 1.13 read the current folder alone, so a
    `prepare` run from another folder was invisible to the build. The home folder is left out while GEO_MAPPING_HOME is set (tests, spikes)."""
    out: list[Path] = []
    cands = [outputs_dir(), _SPEC_DIR.get(), workdir()]
    if not os.environ.get("GEO_MAPPING_HOME"):
        cands.append(Path.home() / WORKDIR_NAME)
    for c in cands:
        if c is None:
            continue
        try:
            r = c.resolve()
        except OSError:
            r = c
        if r not in out:
            out.append(r)
    return out


def find_cached(*parts) -> Path | None:
    """The first existing file at <root>/<parts...> over cache_roots(), or None."""
    for r in cache_roots():
        p = r.joinpath(*parts)
        if p.is_file():
            return p
    return None


@contextlib.contextmanager
def outputs_in(folder):
    """Within the block, outputs_dir() is FOLDER/.geo-mapping. The caller checks the folder first (service_tools.check_work_folder)."""
    token = _OUTPUTS.set(Path(folder) / WORKDIR_NAME)
    try:
        yield _OUTPUTS.get()
    finally:
        _OUTPUTS.reset(token)


def die(code: int, message: str) -> "None":
    print(message, file=sys.stderr)
    sys.exit(code)


def _mapspec_loader():
    """A SafeLoader that follows YAML 1.2 booleans: only true/false are booleans.

    PyYAML implements YAML 1.1, where the bare words on, off, yes and no are booleans. A MapSpec writes
    `title: {on: true}`, and plain safe_load would turn that key into True. Every reader of a MapSpec
    must use load_mapspec(). Writers that dump YAML quote the key ('on'), which round-trips correctly.
    """
    import re
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    bool_tag = "tag:yaml.org,2002:bool"
    Loader.yaml_implicit_resolvers = {
        ch: [(tag, rx) for tag, rx in rules if tag != bool_tag]
        for ch, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    Loader.add_implicit_resolver(bool_tag, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
    return Loader


def _mapspec_dumper():
    """The writing twin of _mapspec_loader: `on` and `off` stay unquoted keys, and key order is kept."""
    import re
    import yaml

    class Dumper(yaml.SafeDumper):
        pass

    bool_tag = "tag:yaml.org,2002:bool"
    Dumper.yaml_implicit_resolvers = {
        ch: [(tag, rx) for tag, rx in rules if tag != bool_tag]
        for ch, rules in yaml.SafeDumper.yaml_implicit_resolvers.items()
    }
    Dumper.add_implicit_resolver(bool_tag, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
    return Dumper


def dump_mapspec(spec) -> str:
    import yaml
    return yaml.dump(spec, Dumper=_mapspec_dumper(), sort_keys=False, allow_unicode=True, default_flow_style=False, width=120)


def load_mapspec_text(text: str):
    import yaml
    return normalize_roles(bool_keys_back(yaml.load(text, Loader=_mapspec_loader())))


def bool_keys_back(obj):
    """1.13.0 (Day 12 issue 7): a MapSpec written back by plain PyYAML (yaml.safe_load, then safe_dump) carries `true:` where it had `on:`,
    because YAML 1.1 reads an unquoted on as true. No MapSpec key is ever a boolean, so a true key is read as `on` and a false one as `off`
    again, and the spec works as it was written."""
    if isinstance(obj, dict):
        return {("on" if k is True else "off" if k is False else k): bool_keys_back(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [bool_keys_back(v) for v in obj]
    return obj


BUILDING_EXTRUDE = {"height_field": "height", "floors_field": "num_floors", "floor_m": 3.5, "default_m": 9}


BUILDING_FILL = "@brand.neutral.2"


def normalize_roles(spec):
    """Role aliases, resolved when a MapSpec is read, so every step sees one vocabulary. 0.24.0: `role: buildings` is footprints drawn as
    polygons, extruded on a 3D page from `height` (then `num_floors` x 3.5 m, then 9 m) unless the layer says otherwise, with the legend
    title "Buildings"."""
    if not isinstance(spec, dict):
        return spec
    for ly in spec.get("layers") or []:
        src = ly.get("source") if isinstance(ly, dict) else None
        if isinstance(src, dict) and src.get("type") == "ref" and ly.get("where") and not src.get("where"):
            # 1.13.0 (Day 12 issue 1): a library service layer's filter written beside the source, not in it; 1.12 dropped it and fetched
            # every feature in the window. It belongs in source.where; it is read from there.
            ly["source"] = {**src, "where": ly.pop("where")}
        if isinstance(ly, dict) and ly.get("role") == "buildings":
            ly["role"] = "polygons"
            ly.setdefault("extrude", dict(BUILDING_EXTRUDE))
            lg = ly.get("legend")
            if not isinstance(lg, dict):
                ly["legend"] = {"title": "Buildings"}
            else:
                lg.setdefault("title", "Buildings")
            ly["_role_asked"] = "buildings"
            sym = ly.get("symbol") if isinstance(ly.get("symbol"), dict) else {}
            if not sym.get("fill"):            # 1.2.0 (Day 10 issue 16): footprints are filled (a mid neutral), so the map and its swatch show them
                ly["symbol"] = dict(sym, fill=BUILDING_FILL)
    return spec


def load_mapspec(path: Path):
    return load_mapspec_text(Path(path).read_text())


def load_yaml(path: Path):
    """Plain YAML for plugin data files (tags, tokens, dependencies). MapSpecs use load_mapspec()."""
    import yaml
    return yaml.safe_load(Path(path).read_text())


def bundled_brands(manifest: dict | None = None) -> list[str]:
    m = manifest or build_manifest()
    return list(m["brands_bundled"])


# 1.7.0: one build, any brand. The plugin ships its default brand in brands/; a brand a person adds is a pack folder kept outside the
# plugin, so a plugin update never removes it. A shipped short name is never taken by an added pack.
BRAND_SLUG = __import__("re").compile(r"^[a-z][a-z0-9-]{1,39}$")
BRANDS_DIRNAME = "brands"


def user_brand_roots() -> list[Path]:
    """Where added brand packs are looked for, first match wins: each folder in GEO_MAPPING_BRANDS (your own pack library, shared by every
    project), the working folder's .geo-mapping/brands/, the folder a tool call named, then your own ~/.geo-mapping/brands/."""
    roots = [Path(x) for x in (os.environ.get("GEO_MAPPING_BRANDS") or "").split(os.pathsep) if x.strip()]
    roots += [workdir() / BRANDS_DIRNAME, outputs_dir() / BRANDS_DIRNAME]
    if not os.environ.get("GEO_MAPPING_HOME"):                 # tests and the persistence spike see only their own folder
        try:
            roots.append(Path.home() / WORKDIR_NAME / BRANDS_DIRNAME)
        except RuntimeError:                                   # no home folder for this process
            pass
    seen, out = set(), []
    for r in roots:
        k = str(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def user_brands() -> dict[str, Path]:
    """{short name: pack folder} of the brand packs a person added (never a shipped name)."""
    shipped = plugin_root() / BRANDS_DIRNAME
    found: dict[str, Path] = {}
    for root in user_brand_roots():
        try:
            dirs = sorted(d for d in root.iterdir() if d.is_dir())
        except OSError:
            continue
        for d in dirs:
            if BRAND_SLUG.match(d.name) and d.name not in found and (d / "PACK.json").is_file() and not (shipped / d.name / "PACK.json").is_file():
                found[d.name] = d
    return found


def available_brands(manifest: dict | None = None) -> list[str]:
    """Every brand a map can name here: the shipped ones first, then the added ones by name."""
    shipped = bundled_brands(manifest)
    return shipped + sorted(s for s in user_brands() if s not in shipped)


def brand_dir(slug: str) -> Path:
    """The pack folder of a brand: the shipped pack, else the first added pack of that name. A name that is not a plain short name (a
    MapSpec is outside input) resolves to a folder that does not exist."""
    shipped = plugin_root() / BRANDS_DIRNAME
    if not isinstance(slug, str) or not BRAND_SLUG.match(slug):
        return shipped / "_invalid_"
    if (shipped / slug / "PACK.json").is_file():
        return shipped / slug
    for root in user_brand_roots():
        if (root / slug / "PACK.json").is_file():
            return root / slug
    return shipped / slug
