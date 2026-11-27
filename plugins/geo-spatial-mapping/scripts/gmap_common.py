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

def utf8_console() -> None:
    """1.17.3: print UTF-8 whatever the console's code page. On a Windows console the default is cp1252, where a report line with an
    arrow or a dot (map_trace.py's) raised UnicodeEncodeError. A stream that is already UTF-8 is left alone; a failure changes nothing."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                   # noqa: BLE001 - a stream that cannot be reconfigured keeps its own encoding
            pass


utf8_console()

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
EDITION_GLOB = "geo-spatial-mapping*"   # 4.0.1: this plugin's name only; the names before 4.0.0 are not looked for


def is_edition_name(name: str) -> bool:
    """A plugin folder or manifest name that is this plugin (4.0.1: geo-spatial-mapping only; a copy left under an older name is ignored)."""
    return str(name or "").startswith("geo-spatial-mapping")


def plugin_root() -> Path:
    """The built edition's root: the parent of scripts/, resolved from this file."""
    return Path(__file__).resolve().parent.parent


def build_manifest() -> dict:
    p = plugin_root() / "BUILD-MANIFEST.json"
    if not p.is_file():
        raise FileNotFoundError("BUILD-MANIFEST.json is missing: this is not a built edition")
    return json.loads(p.read_text(encoding="utf-8"))


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


# ------------------------------------------------------------------------------------------------------------- 3.7.0: the person's own folder
# Spec 3.7.0 Parts A and D: what is the person's (their brand packs, their default brand, their own data sources and service profiles, the
# terms acknowledgment, their licence answers) is written to ONE folder that every project reads, like the credential folder, instead of the
# .geo-mapping/ of whichever folder a session happened to run in. A project's own .geo-mapping/ is still read first, so a client project can
# carry its own brand; `--here` (or scope=project) writes there on purpose.

def ephemeral_home() -> bool:
    """True when the home folder does not last: a Claude Cowork session's Linux VM ($HOME=/sessions/<id>, the person's folders under
    $HOME/mnt). There the folder that lasts is the project folder, and the person's own folder is kept by the plugin's data server on their
    computer (the user_settings tool brings it into a project)."""
    if os.environ.get("GEO_MAPPING_USER"):
        return False
    try:
        h = Path.home()
    except RuntimeError:
        return True
    return str(h).replace("\\", "/").startswith("/sessions/") and (h / "mnt").is_dir()


def user_folder(create: bool = False) -> Path:
    """The person's own folder: ~/.geo-mapping (%USERPROFILE%\\.geo-mapping). GEO_MAPPING_USER stands in for it in tests. While
    GEO_MAPPING_HOME is set (tests, spikes) and GEO_MAPPING_USER is not, it is that working folder, as before; in a session whose home does
    not last (ephemeral_home) it is the working folder too, the one that lasts there."""
    u = os.environ.get("GEO_MAPPING_USER")
    if u:
        p = Path(u)
    elif os.environ.get("GEO_MAPPING_HOME") or ephemeral_home():
        p = workdir()
    else:
        try:
            p = Path.home() / WORKDIR_NAME
        except RuntimeError:
            p = workdir()
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def user_config() -> dict:
    """The person's config.yaml in their own folder ({} when there is none or it cannot be read). Keys: credentials_dir (read by
    credentials.default_dir as before), sources_dir, brands_dir."""
    p = user_folder() / "config.yaml"
    if not p.is_file():
        return {}
    try:
        d = load_yaml(p) or {}
    except Exception:  # noqa: BLE001 - an unreadable config is no config
        return {}
    return d if isinstance(d, dict) else {}


def _config_dir(key: str, default: Path) -> Path:
    v = user_config().get(key)
    return Path(os.path.expanduser(str(v))) if v else default


def user_sources_dir(create: bool = False) -> Path:
    """Where the person's own data sources are kept: `sources_dir:` in config.yaml, else <user folder>/sources (spec 3.7.0 section 3)."""
    p = _config_dir("sources_dir", user_folder() / "sources")
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def user_brands_dir(create: bool = False) -> Path:
    """Where the person's own brand packs are written: `brands_dir:` in config.yaml, else <user folder>/brands (spec 3.7.0 Part D)."""
    p = _config_dir("brands_dir", user_folder() / BRANDS_DIRNAME)
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def settings_places(name: str) -> list[Path]:
    """Where a per-person setting file or folder NAME is read, first match wins: the working folder's .geo-mapping/, the folder a tool call
    named, then the person's own folder (and, outside tests, ~/.geo-mapping). Duplicates removed."""
    out = [workdir() / name, outputs_dir() / name, user_folder() / name]
    if not os.environ.get("GEO_MAPPING_HOME") and not os.environ.get("GEO_MAPPING_USER"):
        try:
            out.append(Path.home() / WORKDIR_NAME / name)
        except RuntimeError:
            pass
    seen, uniq = set(), []
    for p in out:
        k = str(p)
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


_OUTPUTS: "contextvars.ContextVar[Path | None]" = contextvars.ContextVar("gmap_outputs", default=None)


def outputs_dir(create: bool = False) -> Path:
    """Where fetched data, captures and the library cache are written (0.9.4): FOLDER/.geo-mapping when a tool call named a folder (the MCP
    server's work_folder=, so a map built in the person's project folder finds what was fetched), else the working directory. Profiles, host
    confirmations and settings stay in workdir()."""
    p = _OUTPUTS.get() or workdir()
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir(kind: str) -> Path:
    """2.4.0 (I-21, Day 20 issue 3): where a fetch tool puts the data it downloads. Inside a tool call that named a work folder: that folder's
    data/<kind>/ (two levels, beside the maps), not five levels down in .geo-mapping/cache/library/<kind>/. Otherwise the library cache."""
    o = _OUTPUTS.get()
    if o is not None:
        return o.parent / "data" / kind
    return outputs_dir() / "cache" / "library" / kind


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
        if not isinstance(ly, dict):
            continue
        if isinstance(ly.get("palette"), str) and ly["palette"].strip():
            # 5.5.0 (D26-03): `palette: brand_neutral` is the short form of {name: brand_neutral}; the lint used to crash on it
            ly["palette"] = {"name": ly["palette"].strip()}
        lg = ly.get("legend")
        if isinstance(lg, dict) and lg.get("on") is False and "include" not in lg:
            # 5.5.0 (D26-05): `legend: {on: false}` on a layer reads as the element toggle it looks like: the layer is left out of the key
            lg["include"] = False
        cer = str(ly.get("certainty") or "").lower()
        if cer == "reconstructed" and ly.get("role") == "lines":
            # 5.5.0 (Q-04): a reconstructed line is drawn dashed, so it reads as different from an observed one without the legend
            sym = ly.get("symbol") if isinstance(ly.get("symbol"), dict) else {}
            if not sym.get("dash"):
                ly["symbol"] = {**sym, "dash": "dashed"}
        if ly.get("draw") is False and not ly.get("role"):
            ly["role"] = "polygons"             # 2.1.0 (Day 19 issue 9): a helper is not drawn; its role only names its kind of data
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
    return load_mapspec_text(Path(path).read_text(encoding="utf-8"))


def load_yaml(path: Path):
    """Plain YAML for plugin data files (tags, tokens, dependencies). MapSpecs use load_mapspec()."""
    import yaml
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def bundled_brands(manifest: dict | None = None) -> list[str]:
    m = manifest or build_manifest()
    return list(m["brands_bundled"])


# 1.7.0: one build, any brand. The plugin ships its default brand in brands/; a brand a person adds is a pack folder kept outside the
# plugin, so a plugin update never removes it. A shipped short name is never taken by an added pack.
BRAND_SLUG = __import__("re").compile(r"^[a-z][a-z0-9-]{1,39}$")
BRANDS_DIRNAME = "brands"


def parent_workdirs(start: Path | None = None) -> list[Path]:
    """4.0.0 (GSC B-2): the .geo-mapping folders of the folders above the working one, nearest first, up to the first that has one and never
    past the home folder or a drive's root. A map built in project/maps/bellevue/ finds project/.geo-mapping/. Empty while GEO_MAPPING_HOME is
    set (tests and spikes see only their own folder)."""
    if os.environ.get("GEO_MAPPING_HOME"):
        return []
    try:
        cur = (start or Path.cwd()).resolve()
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return []
    out = []
    for p in cur.parents:
        if p == home or p.parent == p:
            break
        if (p / WORKDIR_NAME).is_dir():
            out.append(p / WORKDIR_NAME)
            break
    return out


def brand_search_folders() -> list[str]:
    """The folders searched for brand packs, for a message that says a brand was not found (GSC B-2)."""
    return [str(r) for r in user_brand_roots()]


def user_brand_roots() -> list[Path]:
    """Where added brand packs are looked for, first match wins: each folder in GEO_MAPPING_BRANDS (your own pack library, shared by every
    project), the working folder's .geo-mapping/brands/, the folder a tool call named, then your own ~/.geo-mapping/brands/."""
    roots = [Path(x) for x in (os.environ.get("GEO_MAPPING_BRANDS") or "").split(os.pathsep) if x.strip()]
    roots += [workdir() / BRANDS_DIRNAME, outputs_dir() / BRANDS_DIRNAME]
    roots += [p / BRANDS_DIRNAME for p in parent_workdirs()]   # 4.0.0 (GSC B-2): a build in a subfolder finds the project's packs
    roots += [user_brands_dir()]                               # 3.7.0: brands_dir in config.yaml, else <your folder>/brands (Part D)
    if not os.environ.get("GEO_MAPPING_HOME") and not os.environ.get("GEO_MAPPING_USER"):   # tests and the persistence spike see only their own folder
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


# ------------------------------------------------------------------------------------------------------------- 2.4.0 (X-02): the run ledger
# Every plugin entrypoint writes one line when it exits: script, option names, exit code, duration, outputs written, finding ids and severities,
# items it dropped. map_trace joins the lines to the transcript by time and script, so `| tail`, `> file` and `2>&1` no longer hide a failure or
# an output from the trace. The line holds no option values (paths can carry names) beyond the outputs a script reports itself.
LEDGER_DIRNAME = "traces/runs"
_LEDGER: dict = {}


def ledger_dir() -> Path:
    base = os.environ.get("GMAP_LEDGER_DIR")
    return Path(base) if base else workdir() / LEDGER_DIRNAME


def ledger_file() -> Path:
    import datetime as _dt
    sess = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("GMAP_SESSION")
    name = "".join(ch for ch in (sess or "") if ch.isalnum() or ch in "-_")[:64] or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    return ledger_dir() / f"{name}.ndjson"


def session_ledger_dir() -> Path:
    """3.4.0 (F-2): the one ledger folder of the person, whatever folder a script ran in: GMAP_LEDGER_DIR, else <home>/.geo-mapping/traces/runs
    (GEO_MAPPING_HOME stands in for the home folder in tests). A build run as `cd day21 && build_map.py ...` writes its line here as well as
    under day21/.geo-mapping, so a trace from any folder finds it."""
    base = os.environ.get("GMAP_LEDGER_DIR")
    if base:
        return Path(base)
    home = os.environ.get("GEO_MAPPING_HOME")
    return (Path(home) if home else Path.home() / WORKDIR_NAME) / LEDGER_DIRNAME


# 3.4.0 (F-3): the library entries that write or check an output start the ledger when a session's own script imports them, so a video made
# by render.py through events_anim, timeline and flythrough leaves a line with its outputs, as a plugin entrypoint does.
LAZY_ENTRIES = ("events_anim.build_base", "flythrough.encode", "flythrough.web_copy", "timeline.write_captions", "timeline.assemble",
                "output_lint.check_mp4", "external_qa.run", "build_map.build", "map_export.export", "story.build")


def ledger_start_lazy(entry: str) -> None:
    """Start the ledger on the first call of a plugin library entry (LAZY_ENTRIES) when no plugin entrypoint started it: the line names the
    module as its script, the host program the session wrote, and is written at exit like any other."""
    import atexit
    import datetime as _dt
    import time as _time
    if _LEDGER.get("on") or os.environ.get("GMAP_NO_LEDGER") == "1":
        return
    main = sys.modules.get("__main__")
    host = Path(getattr(main, "__file__", "") or "").name or None
    try:
        cwd = str(Path.cwd())
    except OSError:
        cwd = None
    _LEDGER.update(on=True, lazy=True, script=entry.split(".", 1)[0], entry=entry, host=host,
                   start=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds"), t0=_time.monotonic(),
                   options=[a.split("=", 1)[0] for a in sys.argv[1:] if a.startswith("--")][:16], cwd=cwd, spec=None, out=None)
    real_hook = sys.excepthook

    def _hook(tp, val, tb):
        _LEDGER.update(exit=1, exception=tp.__name__, **crash_info(val, tb))
        real_hook(tp, val, tb)
    sys.excepthook = _hook
    atexit.register(_ledger_write)


def ledger_note(*, outputs=None, findings=None, degraded=None, **extra) -> None:
    """A script adds what it wrote and found to its ledger line (written at exit). Safe to call from library code: it does nothing until the
    process's own entrypoint started the ledger."""
    if not _LEDGER.get("on"):
        return
    if outputs:
        _LEDGER.setdefault("outputs", [])
        for o in outputs:
            s = str(Path(o).resolve()) if o else None
            if s and s not in _LEDGER["outputs"]:
                _LEDGER["outputs"].append(s)
    if findings:
        _LEDGER.setdefault("findings", {})
        for f in findings:
            if isinstance(f, dict) and f.get("id"):
                _LEDGER["findings"][f["id"]] = f.get("severity") or _LEDGER["findings"].get(f["id"])
                if f.get("severity") == "BLOCK" and not _LEDGER.get("first_finding"):
                    _LEDGER["first_finding"] = f"{f['id']}: {str(f.get('message') or '')[:160]}"
    if degraded:
        _LEDGER.setdefault("degraded", [])
        _LEDGER["degraded"] += [str(d) for d in degraded if str(d) not in _LEDGER["degraded"]]
    for k, v in extra.items():
        _LEDGER[k] = v


def _ledger_write() -> None:
    if not _LEDGER.get("on") or _LEDGER.get("written"):
        return
    _LEDGER["written"] = True
    import datetime as _dt
    import time as _time
    row = {"script": _LEDGER["script"], "start": _LEDGER["start"], "end": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds"),
           "seconds": round(_time.monotonic() - _LEDGER["t0"], 2), "exit": _LEDGER.get("exit", 0), "options": _LEDGER["options"],
           "cwd": _LEDGER["cwd"], "pid": os.getpid()}
    tail = _LEDGER.get("_stderr")
    if tail is not None and tail.last and row["exit"] not in (0, None):
        _LEDGER["last_line"] = tail.last[-240:]              # 5.5.0 (T-05): what the script said last, for the trace's one line of cause
    for k in ("outputs", "findings", "degraded", "exception", "spec", "out", "lazy", "entry", "host", "crash_site", "exception_msg", "last_line",
              "first_finding"):
        if _LEDGER.get(k):
            row[k] = _LEDGER[k]
    line = json.dumps(row, default=str) + "\n"
    f = ledger_file()
    targets = [f]
    try:                                                     # 3.4.0 (F-2): the person's one session ledger as well
        s = session_ledger_dir() / f.name
        if s.resolve() != f.resolve():
            targets.append(s)
    except OSError:
        pass
    for t in targets:
        try:
            t.parent.mkdir(parents=True, exist_ok=True)
            with open(t, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:                                      # a read-only folder: the trace falls back to the transcript alone
            pass


def ledger_start() -> None:
    """Called once when this module is imported by a plugin script run as a program (its __main__ is in scripts/, lints/ or mcp/)."""
    import atexit
    import datetime as _dt
    import time as _time
    if _LEDGER.get("on") or os.environ.get("GMAP_NO_LEDGER") == "1":
        return
    main = sys.modules.get("__main__")
    mf = getattr(main, "__file__", None)
    if not mf:
        return
    mp = Path(mf).resolve()
    if mp.parent.name not in ("scripts", "lints", "mcp") or not (mp.parent.parent / "scripts" / "gmap_common.py").is_file():
        return
    import re as _re
    opts, spec, out, argv = [], None, None, sys.argv[1:]
    for i, a in enumerate(argv):
        if a.startswith("--"):
            k = a.split("=", 1)[0]
            if k not in opts:
                opts.append(k)
            val = a.split("=", 1)[1] if "=" in a else (argv[i + 1] if i + 1 < len(argv) else None)
            if k == "--spec" and val:
                spec = val
            elif k == "--out" and val:
                out = val
    try:
        cwd = str(Path.cwd())
    except OSError:
        cwd = None
    _LEDGER.update(on=True, script=mp.stem, start=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds"), t0=_time.monotonic(),
                   options=opts[:16], cwd=cwd, spec=str(Path(spec).resolve()) if spec and _re.search(r"\.ya?ml$", spec) else None,
                   out=str(Path(out).resolve()) if out else None)
    real_exit = sys.exit

    def _exit(code=0):
        _LEDGER["exit"] = code if isinstance(code, int) else (0 if code is None else 1)
        real_exit(code)
    sys.exit = _exit
    real_hook = sys.excepthook

    def _hook(tp, val, tb):
        _LEDGER.update(exit=1, exception=tp.__name__, **crash_info(val, tb))
        real_hook(tp, val, tb)
    sys.excepthook = _hook
    try:                                                     # 5.5.0 (T-05): the last line written to stderr, kept for the ledger
        if not isinstance(sys.stderr, _TailStream):
            _LEDGER["_stderr"] = sys.stderr = _TailStream(sys.stderr)
    except Exception:  # noqa: BLE001 - a ledger never stops a script
        pass
    atexit.register(_ledger_write)


class _TailStream:
    """A stream that passes everything on and remembers its last non-empty line (5.5.0, T-05)."""

    def __init__(self, inner):
        self._inner = inner
        self.last = ""

    def write(self, s):
        try:
            for ln in str(s).splitlines():
                if ln.strip():
                    self.last = ln.strip()
        except Exception:  # noqa: BLE001
            pass
        return self._inner.write(s)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def crash_info(val, tb) -> dict:
    """5.5.0 (T-04): where an exception was raised inside this plugin (the last frame under the plugin's scripts, lints or mcp folder, as
    'lints/mapspec_lint.py:116') and its message, for the run ledger; the trace reads them when the console hid the traceback."""
    import traceback
    site = None
    try:
        root = Path(__file__).resolve().parent.parent
        for fr in traceback.extract_tb(tb):
            p = Path(fr.filename).resolve()
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            if rel.parts and rel.parts[0] in ("scripts", "lints", "mcp", "hooks"):
                site = f"{rel.as_posix()}:{fr.lineno}"
    except Exception:  # noqa: BLE001
        site = None
    out = {"exception_msg": str(val)[:200]}
    if site:
        out["crash_site"] = site
    return out


ROOT_POINTER = "plugin-root.txt"


def root_pointer_path() -> Path:
    home = os.environ.get("GEO_MAPPING_HOME")
    return (Path(home) if home else Path.home() / WORKDIR_NAME) / ROOT_POINTER


def note_root() -> None:
    """3.4.0 (Day 19 #17, Day 21 #13): every plugin script run as a program writes the folder it runs from to ~/.geo-mapping/plugin-root.txt,
    so a script the session wrote reads the plugin's folder from there (newest copy, whatever an update renamed it to, such as ~g2) instead of
    a path pasted earlier that an update can move away."""
    main = sys.modules.get("__main__")
    mf = getattr(main, "__file__", None)
    if not mf or os.environ.get("GMAP_NO_LEDGER") == "1":
        return
    mp = Path(mf).resolve()
    root = plugin_root()
    if mp.parent.parent != root or not (root / "BUILD-MANIFEST.json").is_file():
        return
    try:
        f = root_pointer_path()
        if not f.is_file() or f.read_text(encoding="utf-8").strip() != str(root):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(str(root) + "\n", encoding="utf-8")
    except OSError:
        pass


ledger_start()
note_root()
