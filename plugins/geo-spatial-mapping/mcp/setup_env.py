"""4.0.0 (spec Part B): the private environment's setup, run by setup.cmd (Windows) and setup.sh (macOS, Linux) with the Python findpython chose.

    <python> setup_env.py [all] [/repair] [--root PLUGIN_ROOT] [--base ENV_FOLDER] [--pylog FILE]

Everything it prints also goes to setup.log beside the environment (overwritten each run, `pip freeze` at the end). A failure ends with a cause
code, a one-line fix, the log's path and its last 15 lines; setup.failed holds the code and the fix. The codes (spec 4.0.0 B1):

  NO_PYTHON     no Python passes pycheck.py (written by setup.cmd / setup.sh themselves, which never reach this file then)
  NO_WRITE      the environment's folder cannot be created or written
  PIP_CERT      a CA setting (PIP_CERT, REQUESTS_CA_BUNDLE, SSL_CERT_FILE, CURL_CA_BUNDLE, a pip configuration `cert`) names a missing file
  PIP_TLS       PyPI answers, but its certificate is not trusted here
  NO_NETWORK    pypi.org or files.pythonhosted.org cannot be reached (name lookup, proxy or time-out, said apart)
  VENV_LOCKED   the environment is in use: "Access is denied", "being used by another process", or a data server running from it
  IMPORT        the packages installed but do not import
  PIP           any other pip failure
  PIN_FALLBACK  (a warning) the tested versions do not all install on this Python; the newest that fit were installed instead

A stale CA setting does not stop the run: pip is run once with that setting left out (the person's pip configuration is never changed) and
the summary says so. Nothing here turns certificate checks off or adds a trusted host (spec R3.5).

Also `status()` (read by the stand-in server and the real one's setup_status tool) and the data server's lock files (B8): a server running
from the environment holds server-<pid>.lock, and setup does not change the environment under it.

Standard library only, plain enough for the Python it is judging.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
IMPORTS = "import mcp, httpx, lxml, pyogrio, yaml, jsonschema, shapely, geopandas, pandas, pyarrow, openpyxl"
CA_VARS = ("PIP_CERT", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE")
HOSTS = ("https://pypi.org/simple/pip/", "https://files.pythonhosted.org/")
TAIL = 15
WINGET = "winget install Python.Python.3.13"

FIXES = {
    "NO_PYTHON": "Install Python 3.13: " + WINGET + " (the python and python3 Store stubs are skipped on purpose), then run setup again",
    "NO_WRITE": "The folder {path} cannot be written: free disk space or ask IT to allow it, then run setup again",
    "PIP_CERT": "{setting} points to a missing file: {path}. Fix: pip config unset {key} (or point it to an existing PEM file)",
    "PIP_TLS": ("PyPI's certificate is not trusted here (a proxy that inspects TLS). Use pip 24.2 or later, which reads the Windows "
                "certificate store, or ask IT for the root certificate as a PEM file and run: pip config set global.cert <file>"),
    "NO_NETWORK": "{detail}. If a proxy is needed, set HTTPS_PROXY (or pip config set global.proxy http://host:port), then run setup again",
    "VENV_LOCKED": "Close Claude (and anything else running Python from {venv}), then run: {repair}",
    "IMPORT": "The packages installed but do not import: run  \"{venvpy}\" -m pip check  and see the log",
    "PIP": "pip failed: {detail}. The log has the whole message",
}


def is_windows() -> bool:
    return os.name == "nt"


def base_dir() -> Path:
    """Where the private environment lives, as paths.sh and setup.cmd say."""
    if os.environ.get("GEO_MAPPING_ENV_HOME"):
        return Path(os.environ["GEO_MAPPING_ENV_HOME"])
    if is_windows():
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "AcropolisDocs" / "geo-mapping"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AcropolisDocs" / "geo-mapping"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "acropolisdocs" / "geo-mapping"


def venv_python(base: Path) -> Path:
    return base / "venv" / ("Scripts/python.exe" if is_windows() else "bin/python")


def repair_command(root: Path) -> str:
    return (f'"{root / "mcp" / "setup.cmd"}" /repair' if is_windows() else f'sh "{root / "mcp" / "setup.sh"}" /repair')


class SetupFail(Exception):
    def __init__(self, code: str, fix: str, detail: str = ""):
        super().__init__(fix)
        self.code, self.fix, self.detail = code, fix, detail


# ---------------------------------------------------------------------------------------------------------------- the log
class Log:
    """Prints and keeps every line in setup.log."""

    def __init__(self, path: Path | None):
        self.path = path
        self.f = None
        if path is not None:
            try:
                self.f = open(path, "w", encoding="utf-8")
            except OSError:
                self.f = None

    def __call__(self, *lines: str) -> None:
        for ln in lines:
            print(ln, flush=True)
            if self.f:
                self.f.write(ln + "\n")
                self.f.flush()

    def quiet(self, text: str) -> None:
        """Into the log only (pip's own output)."""
        if self.f and text:
            self.f.write(text if text.endswith("\n") else text + "\n")
            self.f.flush()

    def close(self) -> None:
        if self.f:
            self.f.close()
            self.f = None


def tail(path: Path, n: int = TAIL) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [ln for ln in lines if ln.strip()][-n:]


# ---------------------------------------------------------------------------------------------------------------- lock files (B8)
def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if is_windows():
        import ctypes
        k = ctypes.windll.kernel32                   # never os.kill(pid, 0) on Windows: signal 0 is CTRL_C_EVENT there
        h = k.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259        # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def servers_running(base: Path) -> list[int]:
    """The data servers running from this environment (their lock files); stale lock files are removed."""
    out = []
    for f in sorted(base.glob("server-*.lock")):
        try:
            pid = int(f.stem.split("-", 1)[1])
        except ValueError:
            continue
        if pid_alive(pid):
            out.append(pid)
        else:
            try:
                f.unlink()
            except OSError:
                pass
    return out


def server_started(base: Path) -> Path | None:
    """Called by the data server at start: its lock file, removed again at exit."""
    try:
        base.mkdir(parents=True, exist_ok=True)
        f = base / f"server-{os.getpid()}.lock"
        f.write_text(json.dumps({"pid": os.getpid(), "started": time.strftime("%Y-%m-%d %H:%M:%S"), "python": sys.executable}), encoding="utf-8")
        import atexit

        def _gone(p=f):
            try:
                p.unlink()
            except OSError:
                pass
        atexit.register(_gone)
        return f
    except OSError:
        return None


SERVER_LOG_MAX = 1_000_000


def server_log(base: Path, line: str) -> None:
    """One line in server.log (start, stop, each tool's time and outcome, a crash); kept under about a megabyte."""
    try:
        f = base / "server.log"
        if f.is_file() and f.stat().st_size > SERVER_LOG_MAX:
            f.replace(base / "server.log.1")
        with open(f, "a", encoding="utf-8") as h:
            h.write(time.strftime("%Y-%m-%d %H:%M:%S ") + f"[{os.getpid()}] " + line.replace("\n", " | ")[:2000] + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------------------------- preflight (B2)
def check_write(base: Path) -> dict:
    try:
        base.mkdir(parents=True, exist_ok=True)
        t = base / ".write-test"
        t.write_text("ok", encoding="utf-8")
        t.unlink()
        return {"check": "write access", "ok": True, "detail": str(base)}
    except OSError as ex:
        return {"check": "write access", "ok": False, "code": "NO_WRITE", "detail": f"{base}: {ex}", "fix": FIXES["NO_WRITE"].format(path=base)}


def pip_config(run=subprocess.run, python: str | None = None) -> dict:
    """Every key pip's configuration files set (`pip config list`), as {key: value}; {} when pip cannot say."""
    try:
        p = run([python or sys.executable, "-m", "pip", "config", "list"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for ln in (p.stdout or "").splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def check_ca(env: dict, config: dict) -> list[dict]:
    """A CA setting that names a file that is not there (the incident behind this spec: a PEM in a temp folder, since cleaned up)."""
    found = []
    for var in CA_VARS:
        v = env.get(var)
        if v and not Path(v).exists():
            found.append({"check": "certificates", "ok": False, "code": "PIP_CERT", "setting": var, "path": v, "kind": "env",
                          "fix": FIXES["PIP_CERT"].format(setting=f"The environment variable {var}", path=v, key=var).replace(
                              f"pip config unset {var}", f"remove the {var} variable")})
    for key, v in config.items():
        if key.endswith(".cert") and v and not Path(v).exists():
            found.append({"check": "certificates", "ok": False, "code": "PIP_CERT", "setting": key, "path": v, "kind": "config",
                          "fix": FIXES["PIP_CERT"].format(setting=f"pip's {key} setting", path=v, key=key)})
    return found or [{"check": "certificates", "ok": True, "detail": "no CA setting names a missing file"}]


def classify_net_error(ex: BaseException) -> tuple[str, str]:
    """(code, detail) for a failed reach of PyPI."""
    r = getattr(ex, "reason", ex)
    if isinstance(r, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(r):
        return "PIP_TLS", "the certificate was not trusted"
    if isinstance(r, socket.gaierror):
        return "NO_NETWORK", "the name could not be looked up (no network, or DNS blocked)"
    if isinstance(r, (socket.timeout, TimeoutError)) or "timed out" in str(r):
        return "NO_NETWORK", "the connection timed out"
    if isinstance(ex, urllib.error.HTTPError) and ex.code == 407 or "407" in str(r) or "proxy" in str(r).lower():
        return "NO_NETWORK", "the proxy refused the request (407: it wants a sign-in)"
    if isinstance(r, ConnectionRefusedError) or "refused" in str(r).lower():
        return "NO_NETWORK", "the connection was refused (a firewall or a proxy setting)"
    return "NO_NETWORK", f"{type(r).__name__}: {str(r)[:120]}"


def check_network(urlopen=urllib.request.urlopen, timeout: float = 15.0) -> dict:
    for url in HOSTS:
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "geo-spatial-mapping-setup"})
            with urlopen(req, timeout=timeout):
                pass
        except urllib.error.HTTPError as ex:
            if ex.code < 500 and ex.code != 407:          # the host answered: it can be reached
                continue
            code, detail = classify_net_error(ex)
            return {"check": "network", "ok": False, "code": code, "detail": f"{url.split('/')[2]}: {detail}",
                    "fix": FIXES[code].format(detail=f"{url.split('/')[2]}: {detail}")}
        except Exception as ex:  # noqa: BLE001 - every way a request fails is a reason to report
            code, detail = classify_net_error(ex)
            return {"check": "network", "ok": False, "code": code, "detail": f"{url.split('/')[2]}: {detail}",
                    "fix": FIXES[code].format(detail=f"{url.split('/')[2]}: {detail}")}
    return {"check": "network", "ok": True, "detail": "pypi.org and files.pythonhosted.org answer"}


def python_lines(pylog: Path | None) -> list[str]:
    """The chosen Python, then the ones skipped with why: a header only over a list that has something in it (spec R2.3)."""
    v = sys.version.split()[0]
    out = [f"PASS python: {sys.executable} (Python {v})"]
    if sys.version_info[:2] < (3, 12):
        out.append(f"     Python {v} gets its own tested versions; Python 3.13 is the one setup recommends: {WINGET}")
    skipped = [ln.strip() for ln in (pylog.read_text(encoding="utf-8", errors="replace").splitlines() if pylog and pylog.is_file() else [])
               if ln.strip()]
    if skipped:
        out.append("     Pythons not used:")
        out += [f"       {s}" for s in skipped]
    return out


# ---------------------------------------------------------------------------------------------------------------- pip
def classify_pip(text: str) -> str:
    t = text or ""
    if "Could not find a suitable TLS CA certificate bundle" in t:
        return "PIP_CERT"
    if "CERTIFICATE_VERIFY_FAILED" in t or "certificate verify failed" in t:
        return "PIP_TLS"
    if "Access is denied" in t or "being used by another process" in t or "Permission denied" in t or "Text file busy" in t:
        return "VENV_LOCKED"
    if "ProxyError" in t or "407" in t or "Failed to establish a new connection" in t or "getaddrinfo failed" in t \
            or "NameResolutionError" in t or "Temporary failure in name resolution" in t or "Read timed out" in t:
        return "NO_NETWORK"
    return "PIP"


def last_error(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    errs = [ln for ln in lines if ln.startswith(("ERROR", "error", "OSError", "PermissionError")) or "Error" in ln]
    return (errs or lines or ["no message"])[-1][:240]


class Pip:
    def __init__(self, venvpy: Path, log: Log, env: dict, run=subprocess.run):
        self.venvpy, self.log, self.env, self.run_ = venvpy, log, env, run

    def __call__(self, *args: str, timeout: int = 3600) -> tuple[int, str]:
        cmd = [str(self.venvpy), "-m", "pip", *args, "--disable-pip-version-check", *(["--progress-bar", "off"] if args[:1] == ("install",) else [])]
        try:
            p = self.run_(cmd, capture_output=True, text=True, timeout=timeout, env=self.env)
        except subprocess.TimeoutExpired:
            return 1, "pip did not finish in time (Read timed out)"
        out = (p.stdout or "") + (p.stderr or "")
        self.log.quiet(out)
        return p.returncode, out

    def fail(self, out: str) -> SetupFail:
        code = classify_pip(out)
        detail = last_error(out)
        fix = {"PIP_CERT": FIXES["PIP_CERT"].format(setting="A CA setting", path="(see the log)", key="global.cert"),
               "PIP_TLS": FIXES["PIP_TLS"], "NO_NETWORK": FIXES["NO_NETWORK"].format(detail=detail),
               "VENV_LOCKED": FIXES["VENV_LOCKED"].format(venv=self.venvpy.parent.parent, repair="setup again"),
               "PIP": FIXES["PIP"].format(detail=detail)}[code]
        return SetupFail(code, fix, detail)


# ---------------------------------------------------------------------------------------------------------------- the run
def remove_venv(base: Path, root: Path) -> None:
    v = base / "venv"
    if not v.exists():
        return
    try:
        shutil.rmtree(v)
    except OSError as ex:
        raise SetupFail("VENV_LOCKED", FIXES["VENV_LOCKED"].format(venv=v, repair=repair_command(root)), str(ex)[:200])


def setup(root: Path, base: Path, *, full: bool = False, repair: bool = False, pylog: Path | None = None, run=subprocess.run,
          urlopen=urllib.request.urlopen, env: dict | None = None, log: Log | None = None) -> dict:
    """The whole run; raises SetupFail with the cause. Returns the summary."""
    env = dict(os.environ if env is None else env)
    for k in ("PYTHONHOME", "PYTHONPATH"):
        env.pop(k, None)
    log = log or Log(None)
    venv = base / "venv"
    venvpy = venv_python(base)
    stamp, fullstamp = base / "requirements.installed", base / "full.installed"
    summary = {"python": sys.version.split()[0], "pinned": True, "notes": []}

    log("Checks before installing:")
    w = check_write(base)
    log(("PASS" if w["ok"] else "FAIL") + f" write access: {w['detail']}")
    if not w["ok"]:
        raise SetupFail("NO_WRITE", w["fix"], w["detail"])
    log(*python_lines(pylog))
    if servers_running(base):
        raise SetupFail("VENV_LOCKED", FIXES["VENV_LOCKED"].format(venv=venv, repair=repair_command(root)),
                        "a data server is running from this environment")
    cfg = pip_config(run, sys.executable)
    ca = check_ca(env, cfg)
    stale = [c for c in ca if not c["ok"]]
    for c in ca:
        log(("PASS" if c["ok"] else "FAIL") + " certificates: " + (c.get("detail") or c["fix"]))
    if stale:
        # this run only: the stale setting is left out (the variable unset, or pip's configuration files skipped with the proxy kept)
        for c in stale:
            if c["kind"] == "env":
                env.pop(c["setting"], None)
            else:
                env["PIP_CONFIG_FILE"] = os.devnull
                prox = next((v for k, v in cfg.items() if k.endswith(".proxy") and v), None)
                if prox and not env.get("PIP_PROXY"):
                    env["PIP_PROXY"] = prox
        note = ("This run left out " + ", ".join(c["setting"] for c in stale) + " and used the computer's own certificate store; "
                "fix it for good with: " + stale[0]["fix"].split("Fix: ", 1)[-1])
        summary["notes"].append(note)
        log("     " + note)
    net = check_network(urlopen)
    log(("PASS" if net["ok"] else "FAIL") + f" network: {net['detail']}")
    if not net["ok"]:
        if net["code"] == "PIP_TLS" and stale:
            raise SetupFail("PIP_CERT", stale[0]["fix"], net["detail"])
        raise SetupFail(net["code"], net["fix"], net["detail"])

    if repair:
        log("Repair: removing the environment and its markers.")
        remove_venv(base, root)
        for f in (stamp, fullstamp, base / "setup.failed"):
            try:
                f.unlink()
            except OSError:
                pass

    if venvpy.exists():
        p = run([str(venvpy), str(HERE / "pycheck.py")], capture_output=True, text=True, timeout=120, env=env)
        if p.returncode != 0:
            log("The existing environment was built on a Python that is not whole: " + (p.stdout or "").strip(), f"Rebuilding it on {sys.executable}.")
            remove_venv(base, root)
    if not venvpy.exists():
        log(f"Creating the private environment in {venv}")
        p = run([sys.executable, "-m", "venv", str(venv)], capture_output=True, text=True, timeout=600, env=env)
        log.quiet((p.stdout or "") + (p.stderr or ""))
        if p.returncode != 0:
            out = (p.stdout or "") + (p.stderr or "")
            if classify_pip(out) == "VENV_LOCKED":
                raise SetupFail("VENV_LOCKED", FIXES["VENV_LOCKED"].format(venv=venv, repair=repair_command(root)), last_error(out))
            raise SetupFail("PIP", f"The environment could not be created: {last_error(out)}", last_error(out))

    pip = Pip(venvpy, log, env, run)
    log("Installing packages. The first run downloads about a hundred megabytes and can take a few minutes.")
    rc, out = pip("install", "--upgrade", "pip", timeout=900)
    if rc != 0:                                       # R3.4: a pip that works is enough
        rc2, _ = pip("--version", timeout=120)
        if rc2 != 0:
            raise pip.fail(out)
        log("WARN pip could not be upgraded (" + last_error(out) + "); going on with the pip there is.")
    cons = root / "constraints.txt"
    rc, out = pip("install", "-r", str(HERE / "requirements.txt"), *(["-c", str(cons)] if cons.is_file() else []))
    if rc != 0 and cons.is_file() and classify_pip(out) == "PIP":
        log(f"PIN_FALLBACK Tested versions are unavailable for Python {sys.version_info[0]}.{sys.version_info[1]}; installing the newest that fit. "
            f"Python 3.13 gives the tested set: {WINGET}. Report problems with map-doctor.")
        summary["pinned"] = False
        rc, out = pip("install", "-r", str(HERE / "requirements.txt"))
    if rc != 0:
        raise pip.fail(out)
    p = run([str(venvpy), "-c", IMPORTS], capture_output=True, text=True, timeout=300, env=env)
    if p.returncode != 0:
        log.quiet((p.stdout or "") + (p.stderr or ""))
        raise SetupFail("IMPORT", FIXES["IMPORT"].format(venvpy=venvpy), last_error((p.stderr or "") + (p.stdout or "")))
    shutil.copyfile(HERE / "requirements.txt", stamp)

    if full:
        log("Installing the full map-building set (only needed to build maps in this computer's own Python; in Cowork maps are built in "
            "the session's Linux shell).")
        rc, out = pip("install", "-r", str(root / "requirements.txt"), *(["-c", str(cons)] if cons.is_file() else []))
        if rc != 0 and cons.is_file() and classify_pip(out) == "PIP":
            log("PIN_FALLBACK the full set: the tested versions are unavailable here; installing the newest that fit.")
            summary["pinned"] = False
            rc, out = pip("install", "-r", str(root / "requirements.txt"))
        if rc != 0:
            raise pip.fail(out)
        log("Installing the optional extras and Playwright's Chromium.")
        p = run([str(venvpy), str(HERE / "install_extras.py"), str(root / "requirements-optional.txt")], capture_output=True, text=True,
                timeout=3600, env=env)
        log(*[ln for ln in (p.stdout or "").splitlines() if ln.strip()][-4:])
        shutil.copyfile(root / "requirements.txt", fullstamp)
        p = run([str(venvpy), "-c", "import weasyprint"], capture_output=True, text=True, timeout=120, env=env)
        if p.returncode != 0 and is_windows():
            summary["notes"].append("PDF: WeasyPrint is installed but cannot load GTK here, so this computer's own Python cannot make PDFs. That "
                                    "does not matter in Cowork, where maps (and their PDFs) are made in the session's Linux shell; the data "
                                    "server never makes PDFs.")
    _, freeze = pip("freeze", timeout=300)
    summary["packages"] = len([ln for ln in freeze.splitlines() if "==" in ln])
    return summary


def finish_failed(base: Path, log: Log, ex: SetupFail) -> None:
    try:
        (base / "setup.failed").write_text(f"{ex.code}\n{ex.fix}\n", encoding="utf-8")
    except OSError:
        pass
    log("", f"Setup did not finish: {ex.code}", f"Fix: {ex.fix}")
    if ex.detail:
        log(f"Cause: {ex.detail}")
    lp = base / "setup.log"
    log("", f"The last lines of {lp}:")
    log.close()
    for ln in tail(lp):
        print("  " + ln)
    print(f"\nLog: {lp}", flush=True)


def main(argv: list[str]) -> int:
    args = [a for a in argv]
    full = any(a.lower() == "all" for a in args)
    repair = any(a.lower() in ("/repair", "repair", "--repair") for a in args)

    def opt(name):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None
    root = Path(opt("--root") or HERE.parent).resolve()
    base = Path(opt("--base") or base_dir())
    pylog = Path(opt("--pylog")) if opt("--pylog") else None
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    log = Log(base / "setup.log" if base.is_dir() else None)
    running = base / "setup.running"
    try:
        running.write_text(f"started {os.getpid()}\n", encoding="utf-8")
        (base / "setup.failed").unlink()
    except OSError:
        pass
    log(f"geo-spatial-mapping setup {time.strftime('%Y-%m-%d %H:%M:%S')} ({'repair, ' if repair else ''}{'full set' if full else 'data server'})")
    try:
        s = setup(root, base, full=full, repair=repair, pylog=pylog, log=log)
    except SetupFail as ex:
        finish_failed(base, log, ex)
        return 1
    finally:
        try:
            running.unlink()
        except OSError:
            pass
    log("", f"Done: Python {s['python']}, {s['packages']} packages, " + ("the tested versions." if s["pinned"] else "the newest versions that fit (PIN_FALLBACK)."))
    for n in s["notes"]:
        log("Note: " + n)
    log(f"Log: {base / 'setup.log'}", "Restart Claude to load the map tools now (without a restart it reconnects in about 15 minutes).")
    log.close()
    return 0


# ---------------------------------------------------------------------------------------------------------------- status (B6, B7)
def status(base: Path | None = None, root: Path | None = None, *, serving: bool = False, run=subprocess.run) -> dict:
    """What the setup is doing or did, for the stand-in server, the real server's setup_status tool and map-doctor --setup."""
    base = Path(base or base_dir())
    root = Path(root or HERE.parent)
    log = base / "setup.log"
    venvpy = venv_python(base)
    stamp = base / "requirements.installed"
    out = {"environment": str(base / "venv"), "log": str(log), "log_tail": tail(log), "launch_log_tail": tail(base / "launch.log", 8),
           "server_log_tail": tail(base / "server.log", 8), "repair": repair_command(root)}
    try:
        server_set = stamp.is_file() and stamp.read_bytes() == (root / "mcp" / "requirements.txt").read_bytes()
    except OSError:
        server_set = False
    try:
        fs = base / "full.installed"
        out["full_set"] = ("current" if fs.is_file() and fs.read_bytes() == (root / "requirements.txt").read_bytes()
                           else "out of date" if fs.is_file() else "not installed")
    except OSError:
        out["full_set"] = "unknown"
    failed = base / "setup.failed"
    running = base / "setup.running"
    if serving:
        out.update(state="ready", message="The data server is running from its private environment.")
        if out["full_set"] != "current":
            out["note"] = ("The full map-building set for this computer's own Python is " + out["full_set"] + ". It is needed only to build "
                           "maps in that Python (not in Cowork); install it with Claude closed: " + repair_command(root).replace("/repair", "all"))
        return out
    if running.is_file():
        out.update(state="installing", message="The packages are being installed in a setup window. Restart Claude when it says Done.")
    elif failed.is_file():
        lines = failed.read_text(encoding="utf-8", errors="replace").splitlines()
        code = (lines[0].strip() if lines else "") or "PIP"
        out.update(state="failed", cause=code if code != "failed" else "PIP", fix=(lines[1].strip() if len(lines) > 1 else
                   "Run the setup by hand to see why: " + repair_command(root).replace(" /repair", "")),
                   message="The package install failed. Follow the fix, then run the repair command and restart Claude.")
    elif venvpy.exists() and server_set:
        ok = False
        try:
            ok = run([str(venvpy), "-c", IMPORTS], capture_output=True, timeout=120).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        out.update(state="needs_restart" if ok else "failed", cause=None if ok else "IMPORT",
                   message=("Setup has finished. Restart Claude to load the map tools." if ok
                            else "The packages are installed but do not import: run the repair command, then restart Claude."))
    else:
        out.update(state="not_installed", message="The packages are not installed yet; the setup window starts at the next start of Claude, "
                                                  "or run: " + repair_command(root).replace(" /repair", ""))
    return out


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
