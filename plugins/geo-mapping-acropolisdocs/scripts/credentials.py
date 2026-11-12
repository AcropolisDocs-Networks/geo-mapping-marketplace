#!/usr/bin/env python3
"""credentials: each person's own API keys and logins, kept in a private folder on their own computer (credentials spec Draft 2, release 0.6.0).

A credential is a reference, never a value: `cred:NAME` names an entry in the registry `credentials.yaml`, which says which file holds the
secret, the one host it may be sent to, how it is sent (query parameter, header, bearer token or HTTP Basic), how the service says no, and its
class (`secret`: never written into any output; `browser`: a tile key the provider designs to be public, which may go into a web page once the
person has restricted it to their domain). `apikey:PATH`, `netrc:PATH` and `file:PATH` name a file directly. `env:` and inline values are refused.

Where it is read (the reach decision, spec §5.4): the plugin's MCP server runs as the person on their computer, reads the key at request time,
checks it, sends it, masks it in everything it returns and drops it (primary, C). When that server is not running, the person may connect the
credential folder to the session and these scripts read the key in-process (fallback, A): never on a command line, in an environment variable
or a printed command. Whatever this module learns about a credential file is a handful of yes/no answers, never its content.

    credentials.py status                               names, hosts, classes, status, dates; never a value
    credentials.py add NAME [--class browser] [--confirm-restricted] [--host H --inject query:key|header:NAME|header:NAME:PREFIX|bearer|basic|path|user=header:A,key=header:B [--kind userkey]|postgres:database=D,user=U[,port=P][,sslmode=S]] [--expires YYYY-MM-DD]
    credentials.py check NAME [--probe --confirm-probe HOST]
    credentials.py rotate NAME [--finish] [--probe --confirm-probe HOST]
    credentials.py remove NAME [--delete-files]
    credentials.py audit [FOLDER]                        secret patterns (and, where the credential folder is here, exact values) by file and line
    credentials.py scan PATH... [--allow FILE]           the release-gate secret scan (MAP-CRED-010)
    every command takes --dir FOLDER (the credential folder, for the connected-folder fallback) and --json

Rules: MAP-CRED-001 to 013 (spec §11). Exit 0 fine, 3 a BLOCK, 2 usage, 4 refused, 5 the service answered with an error.
"""
from __future__ import annotations

import argparse
import contextlib
import contextvars
import datetime as dt
import fnmatch
import json
import os
import re
import stat
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402
import redact  # noqa: E402

REGISTRY = "credentials.yaml"
KINDS = ("basic", "apikey", "bearer", "userkey", "postgres")      # userkey: a username and a key, each sent to its own place (0.13.0); postgres: a
                                                                 # read-only database login, opened only by the MCP server on this computer (0.15.0)
PG_FIELDS = ("database", "user", "port", "sslmode", "schema")
PG_SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
PG_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,62}$")
DB_HOST_RE = re.compile(r"^(?:localhost|[a-z0-9](?:[a-z0-9-]{0,62})(?:\.[a-z0-9](?:[a-z0-9-]{0,62}))*|\d{1,3}(?:\.\d{1,3}){3})$")
CLASSES = ("secret", "browser")
STATUSES = ("unchecked", "ready", "rejected", "expired", "missing")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}\.)+[a-z]{2,63}$")
HEADER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")       # letters, digits, - and _ (the FCC BDC header is hash_value)
RESERVED_HEADERS = ("host", "cookie", "content-length", "transfer-encoding", "connection")      # never set from a registry
MIN_USER_MASK = 6                       # a username shorter than this is not masked by value: it would hide ordinary words in every output
PARAM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,31}$")    # 0.14.0: a scheme word before the key in a header (Stadia-Auth, OAuth, Token)
PATH_SLOT = "{key}"                                          # 0.14.0: where a key that sits in the address path goes (NASA FIRMS)
SID_SYSTEM, SID_ADMINS = "S-1-5-18", "S-1-5-32-544"
OLD_FILE_DAYS = 7                      # status reminds about the old file this long after a rotation (spec §8.3)
EXPIRY_WARN_DAYS = 14                  # 0.14.0: a token with an end date (expires) is flagged this long before it, and refused after it
REF_KINDS = ("cred", "apikey", "netrc", "file")
PATH_RE = re.compile(r"^(?:\$HOME|~|[A-Za-z]:)?[\\/][A-Za-z0-9._\\/@+= -]+$")
# A connected folder seen from the device shell can report invented permissions: every file mode 700, chmod ignored (checked on the owner's
# Windows machine, 2026-11-05). On these file systems privacy is not verifiable from here (MAP-CRED-012), never "yes".
SYNTHETIC_FS = ("9p", "virtiofs", "drvfs", "vboxsf", "vmhgfs", "prl_fs", "cifs", "smb3", "smbfs", "ntfs", "ntfs3", "vfat", "msdos", "exfat")


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def blocks(fs: list[dict]) -> list[dict]:
    return [f for f in fs if f["severity"] == "BLOCK"]


def today() -> dt.date:
    v = os.environ.get("GMAP_FIXED_DATE")
    return dt.date.fromisoformat(v) if v else dt.date.today()


def _date(v) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


class CredentialError(Exception):
    """A credential cannot be used; `findings` say why (the first BLOCK is the reason)."""

    def __init__(self, findings: list[dict]):
        super().__init__(findings[0]["message"] if findings else "credential refused")
        self.findings = findings
        self.rule = findings[0]["id"] if findings else "MAP-CRED-001"


# ---------------------------------------------------------------------------------------------------------------- value scope (spec R7)
_VALUES: contextvars.ContextVar = contextvars.ContextVar("gmap_credential_values", default=None)
_LOOSE: list[str] = []                 # values read outside any scope (a script run): masked in what that script prints


def note_value(v: str) -> None:
    lst = _VALUES.get()
    (lst if lst is not None else _LOOSE).append(v)


class ValueScope:
    """The exact values read while it is open. `clean()` masks them, then scrubs the patterns, in a result about to leave the process."""

    def __init__(self):
        self.values: list[str] = []

    def clean(self, obj):
        out = redact.mask_obj(obj, self.values + _LOOSE)
        out, _ = redact.scrub_obj(out)
        return out


@contextlib.contextmanager
def value_scope():
    scope = ValueScope()
    tok = _VALUES.set(scope.values)
    try:
        yield scope
    finally:
        _VALUES.reset(tok)


def masked_text(text: str) -> str:
    """A script's own output, masked with every value it read and scrubbed."""
    lst = (_VALUES.get() or []) + _LOOSE
    return redact.scrub(redact.mask(text, lst))[0]


# ---------------------------------------------------------------------------------------------------------------- the platform seam
@dataclass
class Ace:
    kind: str            # allow | deny | other
    sid: str
    mask: int = 0
    inherited: bool = False


@dataclass
class AclInfo:
    owner: str | None
    protected: bool      # SE_DACL_PROTECTED: inheritance removed
    aces: list = field(default_factory=list)
    null_dacl: bool = False


class WinSecurity:
    """The Win32 security calls, through ctypes (Windows only). Read: GetNamedSecurityInfoW, then the DACL's ACEs one by one, SIDs as strings.
    Nothing here parses localised icacls text. Tests replace this object with one that returns AclInfo values."""

    SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION, DACL_SECURITY_INFORMATION = 1, 0x1, 0x4
    SE_DACL_PROTECTED, INHERITED_ACE, TOKEN_QUERY, TOKEN_USER, ACL_SIZE_INFORMATION = 0x1000, 0x10, 0x0008, 1, 2

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.c, self.w = ctypes, wintypes
        self.adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.adv.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                                   ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
        self.adv.GetNamedSecurityInfoW.restype = wintypes.DWORD
        self.adv.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
        self.adv.GetSecurityDescriptorControl.restype = wintypes.BOOL
        self.adv.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
        self.adv.GetAclInformation.restype = wintypes.BOOL
        self.adv.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        self.adv.GetAce.restype = wintypes.BOOL
        self.adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        self.adv.ConvertSidToStringSidW.restype = wintypes.BOOL
        self.adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        self.adv.OpenProcessToken.restype = wintypes.BOOL
        self.adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.adv.GetTokenInformation.restype = wintypes.BOOL
        self.k32.GetCurrentProcess.restype = wintypes.HANDLE
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k32.LocalFree.argtypes = [ctypes.c_void_p]
        self.k32.LocalFree.restype = ctypes.c_void_p

    def _sid_str(self, psid) -> str:
        s = self.w.LPWSTR()
        if not self.adv.ConvertSidToStringSidW(psid, self.c.byref(s)):
            raise OSError(self.c.get_last_error(), "ConvertSidToStringSidW failed")
        try:
            return s.value
        finally:
            self.k32.LocalFree(s)

    def current_sid(self) -> str:
        c, w = self.c, self.w
        tok = w.HANDLE()
        if not self.adv.OpenProcessToken(self.k32.GetCurrentProcess(), self.TOKEN_QUERY, c.byref(tok)):
            raise OSError(c.get_last_error(), "OpenProcessToken failed")
        try:
            need = w.DWORD(0)
            self.adv.GetTokenInformation(tok, self.TOKEN_USER, None, 0, c.byref(need))
            buf = c.create_string_buffer(need.value)
            if not self.adv.GetTokenInformation(tok, self.TOKEN_USER, buf, need, c.byref(need)):
                raise OSError(c.get_last_error(), "GetTokenInformation failed")
            psid = c.cast(buf, c.POINTER(c.c_void_p))[0]          # TOKEN_USER.User.Sid is the first field
            return self._sid_str(psid)
        finally:
            self.k32.CloseHandle(tok)

    def read(self, path: str) -> AclInfo:
        c, w = self.c, self.w
        owner, dacl, sd = c.c_void_p(), c.c_void_p(), c.c_void_p()
        rc = self.adv.GetNamedSecurityInfoW(str(path), self.SE_FILE_OBJECT, self.OWNER_SECURITY_INFORMATION | self.DACL_SECURITY_INFORMATION,
                                            c.byref(owner), None, c.byref(dacl), None, c.byref(sd))
        if rc != 0:
            raise OSError(rc, "GetNamedSecurityInfoW failed")
        try:
            ctrl, rev = w.WORD(0), w.DWORD(0)
            if not self.adv.GetSecurityDescriptorControl(sd, c.byref(ctrl), c.byref(rev)):
                raise OSError(c.get_last_error(), "GetSecurityDescriptorControl failed")
            info = AclInfo(owner=self._sid_str(owner) if owner.value else None, protected=bool(ctrl.value & self.SE_DACL_PROTECTED))
            if not dacl.value:
                info.null_dacl = True
                return info

            class SizeInfo(c.Structure):
                _fields_ = [("AceCount", w.DWORD), ("AclBytesInUse", w.DWORD), ("AclBytesFree", w.DWORD)]

            class Header(c.Structure):
                _fields_ = [("AceType", c.c_ubyte), ("AceFlags", c.c_ubyte), ("AceSize", w.WORD)]

            si = SizeInfo()
            if not self.adv.GetAclInformation(dacl, c.byref(si), c.sizeof(si), self.ACL_SIZE_INFORMATION):
                raise OSError(c.get_last_error(), "GetAclInformation failed")
            for i in range(si.AceCount):
                pace = c.c_void_p()
                if not self.adv.GetAce(dacl, i, c.byref(pace)):
                    raise OSError(c.get_last_error(), "GetAce failed")
                hdr = Header.from_address(pace.value)
                inherited = bool(hdr.AceFlags & self.INHERITED_ACE)
                if hdr.AceType in (0, 1, 9, 10):                     # ACCESS_ALLOWED, ACCESS_DENIED and their callback forms: Mask at +4, SID at +8
                    mask_v = w.DWORD.from_address(pace.value + 4).value
                    sid = self._sid_str(c.c_void_p(pace.value + 8))
                    info.aces.append(Ace("allow" if hdr.AceType in (0, 9) else "deny", sid, mask_v, inherited))
                else:                                                # an object ACE or anything else: not read here, so it fails the rule
                    info.aces.append(Ace("other", f"type-{hdr.AceType}", 0, inherited))
            return info
        finally:
            self.k32.LocalFree(sd)


class Platform:
    """What the checks need from the machine. `windows` is True on the person's Windows computer (the MCP server); `win` is the Win32 seam;
    `mounts` the text of /proc/mounts (or None); `run` runs an argument list (icacls) and returns its exit code. Tests build one by hand."""

    def __init__(self, *, windows: bool | None = None, win=None, mounts: str | None = None, run=None, env: dict | None = None, known_folders=None):
        self.windows = (os.name == "nt") if windows is None else windows
        self._win = win
        self._mounts = mounts
        self._run = run
        self.env = dict(os.environ) if env is None else env
        self._known = known_folders

    @property
    def win(self):
        if self._win is None:
            self._win = WinSecurity()
        return self._win

    def mounts(self) -> str | None:
        if self._mounts is not None:
            return self._mounts
        try:
            return Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def run(self, argv: list[str]) -> int:
        if self._run is not None:
            return self._run(argv)
        return subprocess.run(argv, capture_output=True, text=True, timeout=60).returncode

    def known_folders(self) -> dict:
        """Desktop, Documents and Pictures as Windows records them (a OneDrive backup moves them); {} elsewhere."""
        if self._known is not None:
            return self._known
        if not self.windows:
            return {}
        try:
            import winreg
            out = {}
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                for name in ("Desktop", "Personal", "My Pictures"):
                    try:
                        v, _ = winreg.QueryValueEx(k, name)
                        out[name] = os.path.expandvars(v)
                    except OSError:
                        continue
            return out
        except Exception:  # noqa: BLE001 - no registry, no moved folders to report
            return {}


# ---------------------------------------------------------------------------------------------------------------- privacy rules
def windows_problems(info: AclInfo, user_sid: str) -> list[str]:
    """R8: inheritance removed; entries only for the current user, SYSTEM and Administrators (matched by SID); the owner one of those."""
    allowed = {user_sid, SID_SYSTEM, SID_ADMINS}
    out = []
    if info.null_dacl:
        out.append("the file has no access list at all, so every account can open it")
        return out
    if not info.protected:
        out.append("inheritance is not removed, so the folder above decides who can read it")
    for a in info.aces:
        if a.kind == "other":
            out.append(f"an access entry of a kind this check does not read ({a.sid})")
        elif a.kind == "allow" and a.sid not in allowed:
            out.append(f"access is granted to {a.sid}" + (" (inherited)" if a.inherited else ""))
        elif a.inherited and a.kind == "allow":
            out.append("an entry is inherited from the folder above")
    if info.owner and info.owner not in allowed:
        out.append(f"the owner is {info.owner}, not you, SYSTEM or Administrators")
    return list(dict.fromkeys(out))


def icacls_commands(path: str, user_sid: str, info: AclInfo | None = None, folder: bool = False) -> list[list[str]]:
    """The icacls argument lists that apply R8, accounts named by SID (*S-1-...), never by localised name. Grant first, then drop inheritance,
    then remove any other account the access list still names."""
    flags = "(OI)(CI)F" if folder else "F"
    cmds = [["icacls", str(path), "/grant:r", f"*{user_sid}:{flags}", f"*{SID_SYSTEM}:{flags}", f"*{SID_ADMINS}:{flags}"],
            ["icacls", str(path), "/inheritance:r"]]
    others = sorted({a.sid for a in (info.aces if info else []) if a.kind == "allow" and a.sid not in (user_sid, SID_SYSTEM, SID_ADMINS)})
    if others:
        cmds.append(["icacls", str(path), "/remove:g", *[f"*{s}" for s in others]])
    return cmds


def shell_quote_win(s: str) -> str:
    return '"' + s.replace('"', '') + '"'


def windows_fix_text(path: str, user_sid: str | None, info: AclInfo | None, folder: bool = False) -> str:
    sid = user_sid or "<your SID: whoami /user>"
    return " && ".join(" ".join(shell_quote_win(a) if i == 1 else a for i, a in enumerate(c)) for c in icacls_commands(path, sid, info, folder))


def powershell_view(path_hint: str) -> str:
    """What the person runs on Windows to see the access list themselves (shown when it cannot be read from here)."""
    return f'(Get-Acl -LiteralPath "{path_hint}").Access | Format-Table IdentityReference,FileSystemRights,IsInherited -AutoSize'


def mount_of(path: Path, mounts_text: str | None) -> tuple[str | None, str | None]:
    """(mount point, file system type) of the longest mount point that holds `path`, from /proc/mounts text."""
    if not mounts_text:
        return None, None
    try:
        p = os.path.realpath(str(path))
    except OSError:
        p = str(path)
    best, fstype = None, None
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mp = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), parts[1])
        if (p == mp or p.startswith(mp.rstrip("/") + "/")) and (best is None or len(mp) > len(best)):
            best, fstype = mp, parts[2]
    return best, fstype


def synthetic_fs(fstype: str | None) -> bool:
    t = (fstype or "").lower()
    return t.startswith("fuse") or t in SYNTHETIC_FS


def _under(p: Path, root: Path) -> bool:
    try:
        a, b = os.path.normcase(os.path.realpath(str(p))), os.path.normcase(os.path.realpath(str(root)))
    except OSError:
        return False
    return a == b or a.startswith(b.rstrip("\\/") + os.sep) or a.startswith(b.rstrip("\\/") + "/")


def synced_by(path: Path, home: Path, plat: Platform) -> str | None:
    """MAP-CRED-004: the cloud-sync service whose folder holds `path`, or None."""
    env = plat.env
    roots: list[tuple[str, Path]] = []
    for k in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer", "ONEDRIVE"):
        if env.get(k):
            roots.append(("OneDrive", Path(env[k])))
    for info in (Path(env.get("APPDATA", "")) / "Dropbox" / "info.json" if env.get("APPDATA") else None,
                 Path(env.get("LOCALAPPDATA", "")) / "Dropbox" / "info.json" if env.get("LOCALAPPDATA") else None, home / ".dropbox" / "info.json"):
        try:
            if info and info.is_file():
                for acct in (json.loads(info.read_text(encoding="utf-8")) or {}).values():
                    if isinstance(acct, dict) and acct.get("path"):
                        roots.append(("Dropbox", Path(acct["path"])))
        except (OSError, ValueError):
            continue
    roots += [("Dropbox", home / "Dropbox"), ("iCloud Drive", home / "iCloudDrive"), ("iCloud Drive", home / "Library" / "Mobile Documents")]
    cs = home / "Library" / "CloudStorage"
    if cs.is_dir():
        roots += [(f"cloud storage ({d.name})", d) for d in cs.iterdir() if d.is_dir()]
    for label, r in roots:
        if _under(path, r):
            return label
    parts = [x.lower() for x in Path(os.path.realpath(str(path))).parts]
    if "my drive" in parts or "mydrive" in parts:
        return "Google Drive"
    if any(x.startswith("onedrive") for x in parts):
        return "OneDrive"
    one = [r for label, r in roots if label == "OneDrive"]
    for name, folder in plat.known_folders().items():
        if folder and any(_under(Path(folder), r) for r in one) and _under(path, Path(folder)):
            return f"OneDrive (your {name} folder is backed up)"
    return None


# ---------------------------------------------------------------------------------------------------------------- locations
def user_home() -> Path:
    """The person's home (%USERPROFILE% on Windows, ~ elsewhere)."""
    return Path.home()


def config_path(home: Path | None = None) -> Path:
    return (home or user_home()) / ".geo-mapping" / "config.yaml"


def default_dir(home: Path | None = None) -> Path:
    """`credentials_dir:` in ~/.geo-mapping/config.yaml, else ~/.geo-mapping/credentials. Environment variables are not used (spec §5.1)."""
    home = home or user_home()
    cp = config_path(home)
    if cp.is_file():
        try:
            import yaml
            v = (yaml.safe_load(cp.read_text(encoding="utf-8")) or {}).get("credentials_dir")
            if v:
                return Path(os.path.expanduser(str(v)))
        except Exception:  # noqa: BLE001 - an unreadable config falls back to the default folder
            pass
    return home / ".geo-mapping" / "credentials"


# ---------------------------------------------------------------------------------------------------------------- library defaults
def entry_block(entry: dict) -> dict | None:
    """The `requires_credential` block of a library entry (delivery or provider), or None."""
    for part in ("delivery", "provider"):
        b = (entry.get(part) or {}).get("requires_credential")
        if isinstance(b, dict):
            return b
    return None


def library_entries() -> list[dict]:
    try:
        import ref_library as rl
        return rl.all_entries("all")
    except Exception:  # noqa: BLE001 - no library here: names must then be added with --host and --inject
        return []


def library_defaults(name: str, entries: list[dict] | None = None) -> tuple[dict | None, dict | None]:
    """(block, entry) for the library entry that declares credential NAME."""
    for e in entries if entries is not None else library_entries():
        b = entry_block(e)
        if b and b.get("name") == name:
            return b, e
    return None, None


def _prefix_ok(p) -> bool:
    """A header prefix is one scheme word; Bearer and Basic have their own forms (bearer, basic)."""
    return isinstance(p, str) and bool(PREFIX_RE.match(p)) and p.lower() not in ("bearer", "basic")


def _target(t) -> dict | None:
    """One place a value is sent: query:NAME, header:NAME or header:NAME:PREFIX as text, or the dict of the registry
    ({query: NAME}, {header: NAME} or, 0.14.0, {header: NAME, prefix: WORD}, sent as "WORD value")."""
    if isinstance(t, str):
        k, _, v = t.strip().partition(":")
        if k == "header" and ":" in v:
            v, _, pre = v.partition(":")
            t = {"header": v, "prefix": pre}
        else:
            t = {k: v}
    if not isinstance(t, dict) or not t:
        return None
    if set(t) == {"header", "prefix"}:
        h = _target({"header": t["header"]})
        return {"header": h["header"], "prefix": t["prefix"]} if h and _prefix_ok(t["prefix"]) else None
    if len(t) != 1:
        return None
    (k, v), = t.items()
    if k == "query" and isinstance(v, str) and PARAM_RE.match(v):
        return {"query": v}
    if k == "header" and isinstance(v, str) and HEADER_RE.match(v) and v.lower() not in RESERVED_HEADERS:
        return {"header": v}
    return None


def _same_place(a: dict, b: dict) -> bool:
    return ("query" in a and "query" in b and a["query"] == b["query"]) or ("header" in a and "header" in b and a["header"].lower() == b["header"].lower())


def parse_inject(text: str | dict | None) -> dict | None:
    """query:key | header:X-API-Key | header:Authorization:Stadia-Auth (a prefix, 0.14.0) | bearer | basic | path (the key in the address path, at
    the request's {key}, 0.14.0), or, for a username and a key (kind userkey), user=header:username,key=header:hash_value; or a dict already in
    registry form ({user: {header: ...}, key: {header: ...}} for the two-part form, {header: NAME, prefix: WORD}, {path: true})."""
    if isinstance(text, dict) and set(text) == {"postgres"}:
        return _pg_inject(text["postgres"])
    if isinstance(text, str) and text.strip().startswith("postgres:"):
        return _pg_inject(text.strip()[len("postgres:"):])
    if isinstance(text, dict):
        if set(text) == {"user", "key"}:
            u, k = _target(text["user"]), _target(text["key"])
            return {"user": u, "key": k} if u and k and not _same_place(u, k) else None
        if set(text) == {"header", "prefix"}:
            return _target(text)
        if len(text) != 1:
            return None
        (k, v), = text.items()
        if k in ("query", "header"):
            return _target({k: v})
        if k in ("bearer", "basic", "path") and v is True:
            return {k: True}
        return None
    if not text:
        return None
    s = str(text).strip()
    if "=" in s:
        parts: dict = {}
        for piece in s.split(","):
            role, eq, tgt = piece.strip().partition("=")
            role = role.strip()
            if not eq or role not in ("user", "key") or role in parts:
                return None
            parts[role] = tgt.strip()
        return parse_inject(parts) if set(parts) == {"user", "key"} else None
    k, _, v = s.partition(":")
    if k in ("bearer", "basic", "path") and not v:
        return {k: True}
    if k == "header" and ":" in v:
        return _target(s)
    return parse_inject({k: v})


def _pg_inject(v) -> dict | None:
    """0.15.0: a database login's connection details: postgres:database=gis,user=reader[,port=5432][,sslmode=require][,schema=public] as text, or
    the registry dict. The password is the only thing in the credential file; these details are not secret and are kept in the registry."""
    if isinstance(v, str):
        d: dict = {}
        for piece in v.split(","):
            k, eq, val = piece.strip().partition("=")
            if not eq or k.strip() in d:
                return None
            d[k.strip()] = val.strip()
        v = d
    if not isinstance(v, dict) or not v.get("database") or not v.get("user") or set(v) - set(PG_FIELDS):
        return None
    out = {"database": str(v["database"]), "user": str(v["user"]), "port": v.get("port", 5432), "sslmode": str(v.get("sslmode") or "prefer")}
    if v.get("schema"):
        out["schema"] = str(v["schema"])
    try:
        out["port"] = int(out["port"])
    except (TypeError, ValueError):
        return None
    if not (PG_NAME_RE.match(out["database"]) and PG_NAME_RE.match(out["user"]) and 0 < out["port"] < 65536 and out["sslmode"] in PG_SSLMODES
            and (not out.get("schema") or PG_NAME_RE.match(out["schema"]))):
        return None
    return {"postgres": out}


def host_ok(host: str, kind: str) -> bool:
    """A web service is named by its public host name; a database (kind postgres, 0.15.0) may also be on this computer or the person's own network."""
    return bool(DB_HOST_RE.match(host or "")) if kind == "postgres" else bool(HOST_RE.match(host or ""))


def inject_fits(kind: str, inj: dict | None) -> bool:
    """A userkey credential is sent as a username and a key; no other kind is. A key in the address path is a plain key (kind apikey). A database
    login (kind postgres) has its connection details and nothing else."""
    if not inj:
        return False
    if ("postgres" in inj) != (kind == "postgres"):
        return False
    return (("user" in inj) == (kind == "userkey")) and (not inj.get("path") or kind == "apikey")


def _inject_text(inj) -> str:
    """The text form of a registry `inject`, for a message (a hand-edited registry can hold anything)."""
    if isinstance(inj, dict):
        if set(inj) == {"user", "key"}:
            return ",".join(f"{r}={_inject_text(inj[r])}" for r in ("user", "key"))
        if set(inj) == {"header", "prefix"}:
            return f"header:{inj['header']}:{inj['prefix']}"
        if len(inj) == 1:
            (k, v), = inj.items()
            return f"{k}:{v}" if isinstance(v, str) else str(k)
        return ""
    return inj if isinstance(inj, str) else ""


def _target_problem(t: str, part: bool = False) -> str | None:
    k, _, v = t.strip().partition(":")
    if k == "header" and ":" in v:
        v, _, pre = v.partition(":")
        if not _prefix_ok(pre):
            return (f"'{pre}' is not a header prefix: one word of letters, digits, . _ and - (Stadia-Auth, OAuth); for Bearer or Basic use bearer or basic"
                    if pre.lower() not in ("bearer", "basic") else f"for {pre} use the {pre.lower()} form, not a prefix")
    if k == "header":
        if v.lower() in RESERVED_HEADERS:
            return f"'{v}' is a header this plugin never sets"
        return None if HEADER_RE.match(v) else f"'{v}' is not a header name this plugin sends (letters, digits, - and _, starting with a letter, at most 64 characters)"
    if k == "query":
        return None if PARAM_RE.match(v) else f"'{v}' is not a query parameter name (letters, digits, _ . -)"
    if part:
        return "each of the username and the key is sent as query:NAME or header:NAME"
    return None if k in ("bearer", "basic", "path") and not v else f"'{t.strip()}' is not one of query:NAME, header:NAME, header:NAME:PREFIX, bearer, basic or path"


def inject_problem(text) -> str:
    """Why an `inject` text was refused, in words the person can act on (MAP-CRED-001). Names the part that is wrong instead of a general refusal."""
    if (isinstance(text, str) and text.strip().startswith("postgres:")) or (isinstance(text, dict) and "postgres" in text):
        return ("a database login is given as postgres:database=NAME,user=NAME with optional port=5432, sslmode=" + "|".join(PG_SSLMODES) +
                " and schema=NAME; names are letters, digits, _ $ and -")
    s = _inject_text(text).strip()
    forms = "query:NAME, header:NAME, header:NAME:PREFIX, bearer, basic, path (the key at {key} in the address path), or for a username and a key user=header:NAME,key=header:NAME"
    if not s:
        return f"say how the credential is sent: {forms}"
    if "=" in s:
        pieces = [p.strip().partition("=") for p in s.split(",")]
        if sorted(p[0].strip() for p in pieces) != ["key", "user"] or not all(p[1] for p in pieces):
            return "a username and a key are given as user=header:NAME,key=header:NAME: one place for each, each named once"
        for role, _, tgt in pieces:
            why = _target_problem(tgt, part=True)
            if why:
                return f"{role.strip()}: {why}"
        return "the username and the key must go to two different places"
    return _target_problem(s) or f"say how the credential is sent: {forms}"


# ---------------------------------------------------------------------------------------------------------------- the store
class Store:
    """The credential folder and its registry. `root` is the folder (default: from the person's home); `plat` the platform seam; `guard_dirs`
    folders the credential must not sit in, as (label, path) pairs, beside the plugin and the working folder."""

    def __init__(self, root: Path | str | None = None, *, home: Path | None = None, plat: Platform | None = None, guard_dirs=(), entries: list[dict] | None = None):
        self.home = Path(home) if home else user_home()
        self.root = Path(root).expanduser() if root else default_dir(self.home)
        self.plat = plat or Platform()
        self.guard_dirs = list(guard_dirs)
        self._entries = entries

    # registry ----------------------------------------------------------------------------------------------------
    @property
    def registry_path(self) -> Path:
        return self.root / REGISTRY

    def exists(self) -> bool:
        return self.registry_path.is_file()

    def entries(self) -> list[dict]:
        if self._entries is None:
            self._entries = library_entries()
        return self._entries

    def load(self) -> dict:
        import yaml
        if not self.registry_path.is_file():
            return {"version": 1, "credentials": {}}
        y = yaml.safe_load(self.registry_path.read_text(encoding="utf-8")) or {}
        y.setdefault("version", 1)
        y["credentials"] = y.get("credentials") or {}
        return y

    def save(self, reg: dict) -> None:
        import yaml
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_path.with_name(REGISTRY + ".tmp")
        tmp.write_text("# geo-mapping credential registry: names and metadata only, never a key, password or token.\n"
                       + yaml.safe_dump(reg, sort_keys=False, default_flow_style=None), encoding="utf-8")
        os.replace(tmp, self.registry_path)
        if not self.plat.windows:
            with contextlib.suppress(OSError):
                os.chmod(self.registry_path, 0o600)

    def get(self, name: str) -> dict | None:
        return self.load()["credentials"].get(name)

    def update(self, name: str, **fields) -> dict:
        reg = self.load()
        e = reg["credentials"].get(name)
        if e is None:
            raise KeyError(name)
        e.update(fields)
        self.save(reg)
        return e

    def path_of(self, e: dict, which: str = "file") -> Path | None:
        f = e.get(which)
        if not f:
            return None
        p = Path(f)
        return p if p.is_absolute() else self.root / p

    def mark_rejected(self, name: str, why: str, url: str | None = None, via: str | None = None) -> None:
        """1.0.2: also which request the service refused (its address as shown, with *** for the key) and through which profile, so the
        message can tell a refusal of the test address from a refusal of some other request."""
        with contextlib.suppress(Exception):
            self.update(name, status="rejected", rejected_on=dt.datetime.now().isoformat(), rejected_why=why,
                        rejected_url=url, rejected_via=via)

    # guarded folders ---------------------------------------------------------------------------------------------
    def guards(self) -> list[tuple[str, Path]]:
        """The plugin folder, the working folder and any folder the caller names. The one exception is the default layout, where the working
        folder is ~/.geo-mapping itself (a session started in the home folder) and credentials/ is its reserved sub-folder."""
        out = [("plugin", gc.plugin_root())]
        wd = gc.workdir()
        default = _try(lambda: (self.home / ".geo-mapping" / "credentials").resolve(), None)
        if not (self.root.resolve() == default and wd.resolve() == default.parent):
            out.append(("working", wd))
        od = gc.outputs_dir()             # 0.9.4: the folder a tool call writes into (work_folder=) holds no key either
        if od != wd and not (self.root.resolve() == default and _try(lambda: od.resolve(), od) == default.parent):
            out.append(("output", od.parent))
        return out + [(lbl, Path(p)) for lbl, p in self.guard_dirs if p]

    # file checks -------------------------------------------------------------------------------------------------
    def check_file(self, path: Path, kind: str = "apikey", shape: str | None = None, host: str | None = None) -> dict:
        """Yes/no answers about one credential file and its folder; the content is read in-process only to judge its shape and is never returned."""
        a = {"exists": False, "regular": False, "private": False, "folder_private": False, "shaped": False, "shape_matches": None, "synced": None,
             "inside": [], "mount_synthetic": False, "privacy_how": None, "problems": [], "error": None}
        p = Path(path)
        a["inside"] = [lbl for lbl, d in self.guards() if _under(p, d) or _under(p.parent, d)]
        a["synced"] = synced_by(p.parent, self.home, self.plat)
        mp, fstype = (None, None) if self.plat.windows else mount_of(p, self.plat.mounts())
        a["mount_synthetic"] = bool(fstype and synthetic_fs(fstype))
        try:
            st_ = os.stat(p)
        except FileNotFoundError:
            return a
        except OSError as ex:
            a["error"] = type(ex).__name__
            return a
        a["exists"] = True
        a["regular"] = stat.S_ISREG(st_.st_mode)
        if self.plat.windows:
            a["privacy_how"] = "windows-acl"
            try:
                sid = self.plat.win.current_sid()
                pf = windows_problems(self.plat.win.read(str(p)), sid)
                pd = windows_problems(self.plat.win.read(str(p.parent)), sid)
                a["private"], a["folder_private"] = not pf, not pd
                a["problems"] = pf + [f"folder: {x}" for x in pd]
            except Exception as ex:  # noqa: BLE001 - an unreadable access list is not private
                a["private"] = a["folder_private"] = None
                a["error"] = f"the access list could not be read ({type(ex).__name__})"
        elif a["mount_synthetic"]:
            a["privacy_how"] = f"not verifiable ({fstype} mount)"
            a["private"] = a["folder_private"] = None
        else:
            a["privacy_how"] = "mode bits"
            try:
                sd = os.stat(p.parent)
                uid = os.getuid() if hasattr(os, "getuid") else None
                a["private"] = (st_.st_mode & 0o077) == 0 and (uid is None or st_.st_uid == uid)
                a["folder_private"] = (sd.st_mode & 0o077) == 0 and (uid is None or sd.st_uid == uid)
            except OSError as ex:
                a["error"] = type(ex).__name__
        if a["regular"]:
            try:
                v = _read_value(p)
                a["shaped"] = _shaped(v, kind)
                if a["shaped"] and shape:
                    with contextlib.suppress(re.error):
                        a["shape_matches"] = bool(re.fullmatch(shape, v.partition(":")[2] if kind == "userkey" else v))
                del v
            except (OSError, UnicodeDecodeError) as ex:
                a["error"] = type(ex).__name__
        return a

    def file_findings(self, name: str, path: Path, a: dict, kind: str, purpose: str = "check") -> list[dict]:
        """MAP-CRED-002/003/004/012 and the shape, with the exact fix."""
        fs: list[dict] = []
        shown = _shown(path, self.home)
        if not a["exists"]:
            fs.append(finding("MAP-CRED-002", "BLOCK", f"the credential file for '{name}' is not there ({shown}); run map-credentials add {name}, or check the folder", shown))
            return fs
        if not a["regular"]:
            fs.append(finding("MAP-CRED-002", "BLOCK", f"{shown} is not a plain file", shown))
            return fs
        if a["inside"]:
            fs.append(finding("MAP-CRED-003", "BLOCK", f"the credential file sits inside the {', '.join(a['inside'])} folder, where it could be copied, packaged or published; "
                                                     "keep it in the credential folder in your home (~/.geo-mapping/credentials)", a["inside"]))
        if a["synced"]:
            fs.append(finding("MAP-CRED-004", "WARN", f"the credential folder is inside a {a['synced']} folder, which copies it to other devices and to the provider; "
                                                    "move it to a folder that is not synced (credentials_dir in ~/.geo-mapping/config.yaml)", a["synced"]))
        if a["private"] is None and a["mount_synthetic"]:
            fs.append(finding("MAP-CRED-012", "WARN", "privacy cannot be verified from here: this folder is reached through a mount whose permissions are made up "
                                                    "(every file shows mode 700). On your Windows computer, check it yourself in PowerShell: "
                                                    + powershell_view(_win_hint(name, path)) + ". Only you, SYSTEM and Administrators should be listed, none inherited",
                              {"privacy": "not verifiable here", "how": a["privacy_how"]}))
        elif a["private"] is None:
            fs.append(finding("MAP-CRED-002", "BLOCK", f"the access list of {shown} could not be read ({a.get('error')}); it is not treated as private", shown))
        elif not (a["private"] and a["folder_private"]):
            if self.plat.windows:
                fix = _try(lambda: windows_fix_text(str(path), self.plat.win.current_sid(), self.plat.win.read(str(path))), "")
                fs.append(finding("MAP-CRED-002", "BLOCK", f"the credential file or its folder can be read by other accounts ({'; '.join(a['problems'][:3])}). "
                                                         f"Run map-credentials check {name} --fix, or in a Command Prompt: {fix}", a["problems"]))
            else:
                fs.append(finding("MAP-CRED-002", "BLOCK", f"the credential file or its folder can be read by other accounts; fix it with: chmod 700 '{path.parent}' && chmod 600 '{path}'",
                                  {"file_private": a["private"], "folder_private": a["folder_private"]}))
        if not a["shaped"]:
            want = {"basic": "one line: user:password", "apikey": "the key alone on one line", "bearer": "the token alone on one line",
                    "userkey": "one line: username:token (your username, a colon, then the token, nothing else)"}.get(kind, "the key alone on one line")
            fs.append(finding("MAP-CRED-002", "BLOCK", f"the credential file for '{name}' should hold {want}; open {shown} in your editor, paste it, save, and check again", shown))
        elif a.get("shape_matches") is False:
            fs.append(finding("MAP-CRED-002", "WARN", f"the key in {shown} does not look like a key for this service; check you pasted the whole key and nothing else", shown))
        return fs

    def fix_privacy(self, path: Path, folder: bool = False) -> bool:
        """Apply the privacy rule to a file or folder: icacls by SID on Windows, chmod elsewhere. True when the rule holds afterwards (or None on
        a synthetic mount, where it cannot be said)."""
        if self.plat.windows:
            sid = self.plat.win.current_sid()
            info = _try(lambda: self.plat.win.read(str(path)), None)
            for argv in icacls_commands(str(path), sid, info, folder):
                if self.plat.run(argv) != 0:
                    return False
            return not windows_problems(self.plat.win.read(str(path)), sid)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700 if folder else 0o600)
        try:
            return (os.stat(path).st_mode & 0o077) == 0
        except OSError:
            return False


def _try(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _shown(path: Path, home: Path) -> str:
    """A path as shown to the person: under the home it starts with ~ (never a value; paths are fine to show)."""
    try:
        return "~/" + Path(path).resolve().relative_to(Path(home).resolve()).as_posix()
    except (ValueError, OSError):
        return str(path)


def _win_hint(name: str, path: Path) -> str:
    return "$env:USERPROFILE\\.geo-mapping\\credentials\\" + Path(path).name


def _read_value(p: Path) -> str:
    t = Path(p).read_bytes()
    if len(t) > 8192:
        raise OSError("the credential file is larger than a key")
    return t.decode("utf-8-sig").strip()


def _shaped(v: str, kind: str) -> bool:
    if not v or "\n" in v or "\r" in v:
        return False
    if kind == "postgres":                 # a database password: one line; spaces and any printable characters are allowed
        return 1 <= len(v) <= 1024 and v.isprintable()
    if kind == "basic":
        u, sep, pw = v.partition(":")
        return bool(sep and u and pw and " " not in u)
    if kind == "userkey":                  # username:token on one line; the username may be an email address, the token may hold colons
        u, sep, tok = v.partition(":")
        return bool(sep and u and 8 <= len(tok) <= 2048 and not re.search(r"\s", v) and v.isprintable() and v.isascii())
    return 8 <= len(v) <= 2048 and not re.search(r"\s", v) and v.isprintable() and v.isascii()


# ---------------------------------------------------------------------------------------------------------------- a resolved credential
def _set_query(url: str, pname: str, value: str) -> str:
    """The URL with query parameter `pname` set to `value`; the rest of the query keeps its own encoding."""
    u = urllib.parse.urlsplit(url)
    segs = [s for s in u.query.split("&") if s and urllib.parse.unquote_plus(s.split("=", 1)[0]) != pname]
    segs.append(urllib.parse.urlencode({pname: value}))
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, "&".join(segs), ""))


def _set_path_key(url: str, value: str) -> str:
    """The URL with the {key} slot of its path filled (0.14.0). The slot must be in the path, once; a URL without it is refused, so a key meant
    for the path is never sent anywhere else. The slot may arrive percent-encoded (%7Bkey%7D) after URL normalisation."""
    u = urllib.parse.urlsplit(url)
    path = re.sub(r"(?i)%7Bkey%7D", PATH_SLOT, u.path)
    if path.count(PATH_SLOT) != 1 or PATH_SLOT in u.query or re.search(r"(?i)%7Bkey%7D", u.query):
        raise CredentialError([finding("MAP-CRED-001", "BLOCK", "this credential is sent in the address path, and the request has no single {key} place in its path")])
    return urllib.parse.urlunsplit((u.scheme, u.netloc, path.replace(PATH_SLOT, urllib.parse.quote(value, safe="")), u.query, ""))


def shown_url(url: str, inj: dict | None) -> str:
    """A request as a run card shows it: the path slot as ***, never a value."""
    return re.sub(r"(?i)\{key\}|%7Bkey%7D", "***", url) if (inj or {}).get("path") else url


BODY_READ = 4096


def body_matches(patterns: list, raw: bytes) -> bool:
    """1.0.2 (F1): any pattern found in the first 4 KB of a body: a plain pattern is a case-insensitive substring, /pattern/ is a regex."""
    text = (raw or b"")[:BODY_READ].decode("utf-8", "replace")
    low = text.lower()
    for p in patterns or []:
        p = str(p)
        if len(p) > 2 and p.startswith("/") and p.endswith("/"):
            with contextlib.suppress(re.error):
                if re.search(p[1:-1], text, re.I):
                    return True
        elif p and p.lower() in low:
            return True
    return False


def effective_reject(name: str, own: dict | None, block: dict | None, store: "Store | None" = None) -> dict:
    """The reject rules a credential is used with (1.0.2): its registered copy, which `add` took from the library, plus the library's
    reject.body when the copy predates it. A body rule only narrows when a declared code counts as a rejection; it never adds one."""
    rj = dict(own or (block or {}).get("reject") or {})
    if not rj.get("body"):
        lib = (block or {}).get("reject") or {}
        if not lib.get("body") and store is not None:
            b, _ = library_defaults(name, _try(store.entries, []))
            lib = (b or {}).get("reject") or {}
        if lib.get("body"):
            rj["body"] = list(lib["body"])
    return rj


class Resolved:
    """One credential, read for one request or one tool call. The value is held privately, noted for masking, and never returned."""

    def __init__(self, name: str, kind: str, host: str, inject: dict, value: str, *, cls: str = "secret", reject: dict | None = None, store: Store | None = None,
                 findings: list[dict] | None = None, ref: str | None = None):
        self.name, self.kind, self.host, self.inject, self.cls = name, kind, host.lower(), inject, cls
        self.reject = reject or {}
        self._u = ""
        if kind == "userkey":               # the file holds username:token; the token is the secret, the username an identifier sent beside it
            self._u, _, value = value.partition(":")
        self._v = value
        self._store = store
        self.findings = findings or []
        self.ref = ref or f"cred:{name}"
        self.was_rejected: str | None = None
        self.via: str | None = None                 # 1.0.2: the service profile this request goes through, when there is one
        self.marks_store = True                     # 1.0.2 (F5): a save-time address check never marks the credential
        note_value(value)
        if kind == "userkey" and len(self._u) >= MIN_USER_MASK:
            note_value(self._u)
        if kind == "basic":
            import base64
            note_value(base64.b64encode(value.encode("utf-8")).decode("ascii"))
        if self.inject.get("path") and urllib.parse.quote(value, safe="") != value:
            note_value(urllib.parse.quote(value, safe=""))     # the form it takes in the address

    def __repr__(self) -> str:                      # never the value
        return f"<credential {self.name} for {self.host}>"

    def record(self) -> dict:
        return {"credential": self.name, "kind": self.kind, "host": self.host}

    def apply(self, url: str, headers: dict) -> tuple[str, dict]:
        """The request URL and headers with the credential added (called by SafeHTTP at send time only)."""
        if self.kind == "postgres":
            raise CredentialError([finding("MAP-CRED-001", "BLOCK", f"'{self.name}' is a database login; it is never sent with a web request")])
        h = dict(headers)
        if "user" in self.inject:                   # a username and a key, each to its own query parameter or header
            for role, val in (("user", self._u), ("key", self._v)):
                t = self.inject[role]
                if "query" in t:
                    url = _set_query(url, t["query"], val)
                else:
                    h[t["header"]] = f"{t['prefix']} {val}" if t.get("prefix") else val
            return url, h
        if "query" in self.inject:                  # the rest of the query keeps its own encoding; only this parameter is replaced
            url = _set_query(url, self.inject["query"], self._v)
        elif "header" in self.inject:
            pre = self.inject.get("prefix")
            h[self.inject["header"]] = f"{pre} {self._v}" if pre else self._v
        elif self.inject.get("path"):
            url = _set_path_key(url, self._v)
        elif self.inject.get("bearer"):
            h["Authorization"] = "Bearer " + self._v
        elif self.inject.get("basic"):
            import base64
            h["Authorization"] = "Basic " + base64.b64encode(self._v.encode("utf-8")).decode("ascii")
        return url, h

    def pg_params(self) -> dict:
        """0.15.0: the keyword arguments for a database connection (postgis_client only, at connect time). Holds the password: never returned."""
        if self.kind != "postgres":
            raise CredentialError([finding("MAP-CRED-001", "BLOCK", f"'{self.name}' is not a database login")])
        p = self.inject["postgres"]
        return {"host": self.host, "port": p["port"], "dbname": p["database"], "user": p["user"], "password": self._v, "sslmode": p["sslmode"]}

    def is_reject_status(self, status: int) -> bool:
        """The codes that mean the key is wrong: the entry's reject.status when it declares one (1.3.1: honoured as declared, so an entry that
        lists only 401 does not count a 403 as a wrong key), else 401 and 403."""
        return status in (self.reject.get("status") or [401, 403])

    def needs_body_match(self, status: int) -> bool:
        """1.0.2 (F1): 401 and 403 are rejections whatever the answer says. Another declared code (FIRMS answers 400 for a wrong key and for a
        wrong address) is a rejection only when the entry also declares reject.body and the answer matches it."""
        return status not in (401, 403) and bool(self.reject.get("body"))

    def body_rejects(self, raw: bytes) -> bool:
        """Whether the first 4 KB of an answer, before masking, say the key is wrong: a case-insensitive substring, or /a regex/."""
        return body_matches(self.reject.get("body") or [], raw)

    def rejection_hint(self) -> str:
        return (f"If you believe the key is fine (a wrong address or layer can look like this), run /{_prefix()}:map-credentials check {self.name} --probe "
                f"to test it once; otherwise get a new key and run /{_prefix()}:map-credentials rotate {self.name}")

    def is_reject_redirect(self, url: str) -> bool:
        u = urllib.parse.urlsplit(url)
        paths = self.reject.get("redirect_path") or []
        return (not u.hostname or u.hostname.lower() == self.host) and any(u.path == p or u.path.endswith(p) for p in paths)

    def rejected(self, why: str, url: str | None = None) -> None:
        self.was_rejected = why
        if self._store is not None and self.ref.startswith("cred:") and self.marks_store:
            self._store.mark_rejected(self.name, why, url, self.via)

    def _masks(self) -> list[str]:
        """The values to hide in anything returned: the secret, and a username long enough not to hide ordinary words."""
        return [self._v] + ([self._u] if self.kind == "userkey" and len(self._u) >= MIN_USER_MASK else [])

    def mask(self, text: str) -> str:
        return redact.mask(text, self._masks())

    def mask_bytes(self, data: bytes) -> bytes:
        """A response body with the value masked: a service that echoes the request (an error page, an XML exception) never hands it back."""
        return redact.mask_bytes(data, self._masks())

    def page_value(self) -> str:
        """The value for a web page: only a `browser`-class key (MAP-CRED-011 otherwise)."""
        if self.cls != "browser":
            raise CredentialError([finding("MAP-CRED-011", "BLOCK", f"'{self.name}' is a secret-class credential; it is never written into a web page or other shared output")])
        return self._v

    def close(self) -> None:
        self._v = self._u = ""


# ---------------------------------------------------------------------------------------------------------------- references
def parse_ref(ref: str) -> tuple[dict | None, list[dict]]:
    """MAP-CRED-001: cred:NAME, apikey:PATH, netrc:PATH or file:PATH; no other form and no inline value. No file is touched."""
    ref = (ref or "").strip()
    kind, sep, rest = ref.partition(":")
    kind = kind.lower()
    if not sep or kind not in REF_KINDS:
        msg = ("env: is not accepted: the plugin's processes cannot see variables you set, and a variable is not a private file" if kind == "env"
               else "a credential reference is cred:NAME (a registered credential), apikey:PATH, netrc:PATH or file:PATH; a key, token or password is never given here")
        return None, [finding("MAP-CRED-001", "BLOCK", msg)]
    rest = rest.strip()
    if kind == "cred":
        if not NAME_RE.match(rest):
            return None, [finding("MAP-CRED-001", "BLOCK", "a credential name is lower-case letters, digits, - and _ (at most 40), such as cred:census")]
        return {"kind": "cred", "name": rest}, []
    if not PATH_RE.match(rest) or ".." in re.split(r"[\\/]", rest) or rest.endswith(("/", "\\")):
        return None, [finding("MAP-CRED-001", "BLOCK", "the credential file must be given as a plain path on your computer (starting with / or ~/); nothing else is accepted")]
    return {"kind": kind, "path": rest}, []


def resolve(ref: str, *, store: Store | None = None, host: str | None = None, block: dict | None = None, purpose: str = "request") -> Resolved:
    """A usable credential, or CredentialError with the findings. `host` is the host the caller will send to (it must equal the registered one,
    MAP-CRED-013); `block` is a library entry's requires_credential, which supplies host and injection for a path reference."""
    r, fs = parse_ref(ref)
    if r is None:
        raise CredentialError(fs)
    store = store or Store()
    if r["kind"] == "cred":
        name = r["name"]
        e = store.get(name)
        if e is None:
            raise CredentialError([not_registered(name, store)])
        kind, ehost, inject, cls = e.get("kind", "apikey"), (e.get("host") or "").lower(), parse_inject(e.get("inject")), e.get("class", "secret")
        path = store.path_of(e)
        reject = effective_reject(name, e.get("reject"), block, store)
        shape = e.get("shape") or (block or {}).get("shape")
    else:
        name = (block or {}).get("name") or Path(r["path"]).stem
        kind = {"apikey": (block or {}).get("kind", "apikey"), "netrc": "basic", "file": "basic"}[r["kind"]]
        ehost = ((block or {}).get("host") or host or "").lower()
        inject = parse_inject((block or {}).get("inject")) or ({"basic": True} if kind == "basic" else None)
        cls = (block or {}).get("class", "secret")
        path = Path(os.path.expanduser(r["path"].replace("$HOME", "~")))
        reject = (block or {}).get("reject") or {}
        shape = (block or {}).get("shape")
        e = {"status": "unchecked"}
        if r["kind"] == "netrc":
            raise CredentialError([finding("MAP-CRED-001", "BLOCK", "netrc: references are for approved runs on your computer (credential-broker); register the login with map-credentials add instead")])
    if not ehost or not host_ok(ehost, kind):
        raise CredentialError([finding("MAP-CRED-013", "BLOCK", f"the credential '{name}' names no host it may be sent to; add it again with --host")])
    if host and host.lower() != ehost:
        raise CredentialError([finding("MAP-CRED-013", "BLOCK", f"the credential '{name}' is registered for {ehost}, but this request goes to {host}; it is never sent to another host")])
    if not inject:
        raw = e.get("inject") if r["kind"] == "cred" else (block or {}).get("inject")
        raise CredentialError([finding("MAP-CRED-001", "BLOCK", f"the credential '{name}' cannot be sent as registered: {inject_problem(raw)}")])
    if not inject_fits(kind, inject):
        raise CredentialError([finding("MAP-CRED-001", "BLOCK", f"the credential '{name}' is a userkey credential, sent as user=header:NAME,key=header:NAME" if kind == "userkey" else
                                       f"the credential '{name}' is kind {kind}, which is not sent as a username and a key; register it with kind userkey for that")])
    if (purpose == "database") != (kind == "postgres"):
        raise CredentialError([finding("MAP-CRED-001", "BLOCK", f"'{name}' is a database login and is used only for a database connection" if kind == "postgres"
                                       else f"'{name}' is not a database login; register one with --kind postgres --inject postgres:database=NAME,user=NAME")])
    if purpose == "page" and cls != "browser":
        raise CredentialError([finding("MAP-CRED-011", "BLOCK", f"'{name}' is a secret-class credential: it is never written into a web map or other shared output. "
                                                              "Use the bundled basemap, a PMTiles file, or register a key the provider designs for browsers with --class browser")])
    ex = _date(e.get("expires"))
    if ex and ex < today():
        raise CredentialError([finding("MAP-CRED-008", "BLOCK", f"the token of '{name}' ended on {ex.isoformat()}; it is not sent. Generate a new token at the service, "
                                                              f"then run map-credentials rotate {name} and add it again with --expires")])
    if e.get("status") == "rejected":
        raise CredentialError([finding("MAP-CRED-008", "BLOCK", f"the service rejected '{name}' ({rejection_text(e)}); runs that need it refuse to start until "
                                                              f"/{_prefix()}:map-credentials check {name} passes. If you believe the key is fine, run "
                                                              f"/{_prefix()}:map-credentials check {name} --probe to test it once; otherwise get a new key and run "
                                                              f"/{_prefix()}:map-credentials rotate {name}")])
    a = store.check_file(path, kind, shape, ehost)
    found = store.file_findings(name, path, a, kind, purpose="use")
    if blocks(found):
        raise CredentialError(blocks(found) + [x for x in found if x["severity"] != "BLOCK"])
    warn = [x for x in found if x["severity"] != "BLOCK"]
    rv = _review_warnings(name, e)
    value = _read_value(path)
    return Resolved(name, kind, ehost, inject, value, cls=cls, reject=reject, store=store if r["kind"] == "cred" else None, findings=warn + rv, ref=ref)


def not_registered(name: str, store: Store) -> dict:
    here = store.exists()
    msg = (f"no credential named '{name}' is registered: run /{_prefix()}:map-credentials add {name}, put your own key in the file it names, then check it"
           if here else
           f"no credential named '{name}' is registered here (no credential folder at {_shown(store.root, store.home)}). On your own computer run "
           f"/{_prefix()}:map-credentials add {name}. In the cloud session the credential folder is not reachable: this fetch runs on your computer through "
           "the plugin's MCP server")
    return finding("MAP-CRED-013", "BLOCK", msg, name)


def _prefix() -> str:
    try:
        return gc.build_manifest()["plugin_name"]
    except Exception:  # noqa: BLE001
        return "geo-mapping-acropolisdocs"


def _review_warnings(name: str, e: dict, now: dt.date | None = None) -> list[dict]:
    """MAP-CRED-009: past review_after; rejected and not rotated; a browser key without the restriction; an old file kept after rotation."""
    now = now or today()
    out = []
    ra = _date(e.get("review_after"))
    if ra and ra < now:
        out.append(finding("MAP-CRED-009", "WARN", f"'{name}' passed its review date ({ra.isoformat()}); check it, or rotate it", name))
    ex = _date(e.get("expires"))
    if ex and now <= ex <= now + dt.timedelta(days=EXPIRY_WARN_DAYS):
        out.append(finding("MAP-CRED-009", "WARN", f"the token of '{name}' ends on {ex.isoformat()} ({(ex - now).days} days); generate a new one and run map-credentials rotate {name}", name))
    if e.get("class") == "browser" and not e.get("restricted"):
        out.append(finding("MAP-CRED-009", "WARN", f"'{name}' is a browser key with no record that it is restricted to your domain; restrict it in the provider's dashboard, "
                                                 f"then run map-credentials add {name} --class browser --confirm-restricted", name))
    rot = _date(e.get("rotated"))
    if e.get("previous_file") and rot and (now - rot).days > OLD_FILE_DAYS:
        out.append(finding("MAP-CRED-009", "WARN", f"the old key file of '{name}' ({e['previous_file']}) is still kept {(now - rot).days} days after the rotation; delete it", name))
    return out


# ---------------------------------------------------------------------------------------------------------------- lifecycle
def _create_private(store: Store, path: Path, folder: bool = False) -> list[dict]:
    """Make a private folder or an empty private file (never overwritten), then apply the privacy rule."""
    fs: list[dict] = []
    if folder:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    elif not path.exists():
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    ok = _try(lambda: store.fix_privacy(path, folder), False)
    if not ok and not store.plat.windows:
        mp, fstype = mount_of(path, store.plat.mounts())
        if fstype and synthetic_fs(fstype):
            return fs
    if not ok:
        fs.append(finding("MAP-CRED-002", "BLOCK", f"the privacy rule could not be applied to {_shown(path, store.home)}; run map-credentials check for the fix"))
    return fs


def add(store: Store, name: str, *, cls: str | None = None, restricted: bool = False, host: str | None = None, inject=None, kind: str | None = None,
        review_after: str | None = None, note: str | None = None, expires: str | None = None) -> dict:
    """Create the folder (private), an empty private file and the registry entry. Never asks for the key: the person opens the file and pastes it."""
    if not NAME_RE.match(name or ""):
        return {"ok": False, "findings": [finding("MAP-CRED-001", "BLOCK", "a credential name is lower-case letters, digits, - and _ (at most 40), such as census")]}
    block, entry = library_defaults(name, store.entries())
    b = dict(block or {})
    host = (host or b.get("host") or "").lower()
    inj = parse_inject(inject) if inject else parse_inject(b.get("inject"))
    pg_text = (isinstance(inject, str) and inject.strip().startswith("postgres:")) or (isinstance(inject, dict) and "postgres" in inject)
    kind = kind or b.get("kind") or ("postgres" if (inj and "postgres" in inj) or pg_text else "userkey" if inj and "user" in inj else "basic" if inj and inj.get("basic") else "apikey")
    allowed_cls = b.get("class_allowed") or ([b["class"]] if b.get("class") else ["secret"])
    cls = cls or b.get("class") or "secret"
    fs: list[dict] = []
    if kind not in KINDS:
        fs.append(finding("MAP-CRED-001", "BLOCK", f"kind is one of {', '.join(KINDS)}"))
    if cls not in CLASSES:
        fs.append(finding("MAP-CRED-001", "BLOCK", f"class is secret or browser, not {cls}"))
    elif cls == "browser" and "browser" not in allowed_cls:
        fs.append(finding("MAP-CRED-011", "BLOCK", f"'{name}' cannot be a browser-class key: only tile-provider keys the provider designs to be public may be"))
    if not host_ok(host, kind):
        fs.append(finding("MAP-CRED-013", "BLOCK", f"'{name}' is not a service the library knows; give the one host it may be sent to (--host api.example.org) and how it is sent (--inject query:key)"))
    if not inj:
        fs.append(finding("MAP-CRED-001", "BLOCK", inject_problem(inject if inject else b.get("inject"))))
    elif kind in KINDS and not inject_fits(kind, inj):
        fs.append(finding("MAP-CRED-001", "BLOCK", "a username + key credential (kind userkey) is sent as user=header:NAME,key=header:NAME" if kind == "userkey" else
                                                   f"a key in the address path is kind apikey, not {kind}" if inj.get("path") else
                                                   f"kind {kind} is not sent as a username and a key; give --kind userkey for that"))
    if kind == "postgres" and cls == "browser":
        fs.append(finding("MAP-CRED-011", "BLOCK", "a database login is always secret class: it is never written into a web page"))
    if kind == "userkey" and cls == "browser":
        fs.append(finding("MAP-CRED-011", "BLOCK", "a username + key credential is always secret class: it is never written into a web page"))
    exp = None
    if expires:
        exp = _date(expires)
        if exp is None:
            fs.append(finding("MAP-CRED-001", "BLOCK", f"expires is a date such as {(today() + dt.timedelta(days=60)).isoformat()}, the day the service says the token ends"))
        elif exp <= today():
            fs.append(finding("MAP-CRED-001", "BLOCK", f"expires ({exp.isoformat()}) is not in the future; generate a new token and give its end date"))
    elif b.get("lifetime_days"):
        exp = today() + dt.timedelta(days=int(b["lifetime_days"]))
    if inj and inj.get("path") and cls == "browser":
        fs.append(finding("MAP-CRED-011", "BLOCK", "a key that sits in the address path is always secret class: the address would carry it into the page"))
    if blocks(fs):
        return {"ok": False, "findings": fs}
    reg = store.load()
    existed = name in reg["credentials"]
    fs += _create_private(store, store.root, folder=True)
    e = dict(reg["credentials"].get(name) or {})
    fname = e.get("file") or f"{name}.key"
    path = store.root / fname
    fresh = not path.exists()
    fs += _create_private(store, path)
    e.update({"kind": kind, "class": cls, "host": host, "file": fname, "inject": inj})
    if b.get("reject") or e.get("reject"):
        e["reject"] = effective_reject(name, e.get("reject"), b)
    if b.get("probe"):
        e["probe"] = b["probe"]
    if b.get("shape"):
        e["shape"] = b["shape"]
    e.setdefault("created", today().isoformat())
    if review_after:
        e["review_after"] = review_after
    else:
        e.setdefault("review_after", (today() + dt.timedelta(days=365)).isoformat())
    if exp:
        e["expires"] = exp.isoformat()
    e.setdefault("status", "unchecked")
    e.setdefault("last_checked", None)
    e.setdefault("previous_file", None)
    if cls == "browser":
        e["restricted"] = today().isoformat() if restricted else e.get("restricted")
    else:
        e.pop("restricted", None)
    if note:
        e["note"] = str(note)[:200]
    elif b.get("note") and not e.get("note"):
        e["note"] = str(b["note"])[:200]
    reg["credentials"][name] = e
    store.save(reg)
    shown = _shown(path, store.home)
    win = str(path) if store.plat.windows else None
    what = ("your username, a colon and your token as the only line (username:token)" if kind == "userkey" else
            "the database password of a read-only account as the only line" if kind == "postgres" else "your own key as the only line")
    nxt = [f"Open {win or shown} in your editor (Notepad on Windows), paste {what}, and save." if fresh or not _try(lambda: _read_value(path), "") else
           f"The file {shown} is already there; it was kept as it is.",
           f"Then run /{_prefix()}:map-credentials check {name}."]
    if cls == "browser" and not restricted:
        nxt.append("This is a browser key: restrict it to your web site's domain in the provider's dashboard, then run add again with --confirm-restricted.")
    if exp:
        nxt.append(f"The token ends on {exp.isoformat()}" + ("" if expires else f" ({b['lifetime_days']} days from today, the service's lifetime); if you made it earlier, run add again with --expires and the date the service shows")
                   + f". Status warns {EXPIRY_WARN_DAYS} days before; after that day runs refuse to send it until you rotate.")
    out = {"ok": not blocks(fs), "name": name, "file": shown, "host": host, "kind": kind, "class": cls, "already_registered": existed, "expires": e.get("expires"),
           "signup_url": b.get("signup_url"), "free": b.get("free"), "library_entry": (entry or {}).get("id"), "next": nxt, "findings": fs,
           "never": "Never paste the key into the chat. If you already did, treat it as exposed: get a new one and rotate.",
           "terms": (f"A key is a customization beyond the plugin's defaults, and it is your own acceptance of {(entry or {}).get('title') or name}'s terms: "
                     "you accepted them to get the key, so the plugin asks nothing more. Keep your use within those terms and your plan; the plugin keeps "
                     "its own limits and prints the credit.")}
    return out


def _row(store: Store, name: str, e: dict, now: dt.date) -> dict:
    path = store.path_of(e)
    exists = bool(path and path.is_file())
    st_ = e.get("status") or "unchecked"
    if not exists:
        st_ = "missing"
    elif st_ != "rejected" and ((_date(e.get("review_after")) and _date(e.get("review_after")) < now) or (_date(e.get("expires")) and _date(e.get("expires")) < now)):
        st_ = "expired"
    created = _date(e.get("created"))
    prev = store.path_of(e, "previous_file")
    a = store.check_file(path, e.get("kind", "apikey"), e.get("shape"), e.get("host")) if exists else None
    privacy = None if a is None else ("yes" if a["private"] and a["folder_private"] else "not verifiable here" if a["private"] is None and a["mount_synthetic"] else "no")
    return {"name": name, "host": e.get("host"), "kind": e.get("kind"), "class": e.get("class", "secret"), "status": st_, "last_checked": e.get("last_checked"),
            "review_after": e.get("review_after"), "expires": e.get("expires"), "age_days": (now - created).days if created else None, "private": privacy,
            "filled": bool(a and a["shaped"]), "old_file_pending": (e.get("previous_file") if prev and prev.is_file() else None),
            "next_file_pending": e.get("next_file"), "restricted": e.get("restricted") if e.get("class") == "browser" else None,
            "findings": _review_warnings(name, e, now) + ([finding("MAP-CRED-009", "WARN", f"'{name}' was rejected and has not been rotated; get a new key and run map-credentials rotate {name}", name)]
                                                       if st_ == "rejected" else [])
                        + ([finding("MAP-CRED-012", "WARN", f"privacy of '{name}' cannot be verified from here", name)] if privacy == "not verifiable here" else [])
                        + ([finding("MAP-CRED-002", "BLOCK", f"the file of '{name}' can be read by other accounts; run map-credentials check {name}", name)] if privacy == "no" else [])}


def status(store: Store, now: dt.date | None = None) -> dict:
    """Every registered credential: name, host, kind, class, status, dates, age, privacy, an old file pending deletion. Never a value."""
    now = now or today()
    reg = store.load()
    rows = [_row(store, n, e, now) for n, e in sorted(reg["credentials"].items())]
    fs = [f for r in rows for f in r["findings"]]
    return {"ok": True, "folder": _shown(store.root, store.home), "folder_found": store.root.is_dir(), "count": len(rows), "credentials": rows, "findings": fs,
            "summary": {k: sum(1 for r in rows if r["status"] == k) for k in STATUSES}}


def probe_url(store: Store, name: str, e: dict) -> str | None:
    """The registered test address, or (0.16.0) the library's when the credential was registered before the library named one; same host only."""
    url = e.get("probe")
    if not url:
        block, _ = library_defaults(name, store.entries())
        url = (block or {}).get("probe") if (block or {}).get("host", "").lower() == (e.get("host") or "").lower() else None
    return url or None


def rejection_text(e: dict) -> str:
    """1.0.2 (F2): what the service refused and where: "HTTP 400 on 2026-11-09 10:05, by a request to https://.../***/ (profile nasa-firms-wfs)"."""
    why = e.get("rejected_why") or "earlier"
    when = str(e.get("rejected_on") or "")[:16].replace("T", " ")
    s = why + (f" on {when}" if when else "")
    if e.get("rejected_url"):
        s += f", by a request to {e['rejected_url']}"
    if e.get("rejected_via"):
        s += f" (profile {e['rejected_via']})"
    return s


def _probe(store: Store, name: str, e: dict, path: Path, probe_host: str, http=None) -> dict:
    """One request to the registered probe address with the credential (consent: the person typed the host back)."""
    import ogc_http as oh
    if e.get("kind") == "postgres":                  # 0.15.0: one read-only connection and SELECT postgis_full_version(), with the same consent
        host = (e.get("host") or "").lower()
        if (probe_host or "").strip().lower() != host:
            return {"probed": False, "needs_consent": True, "ask": f"open one read-only connection to the database on {host} with your '{name}' login?",
                    "then": f"call again with confirm_probe={host}"}
        import postgis_client as pc
        cred = Resolved(name, "postgres", host, parse_inject(e.get("inject")), _read_value(path), store=store, ref=f"cred:{name}")
        try:
            return pc.probe(cred)
        finally:
            cred.close()
    url = probe_url(store, name, e)
    if not url:
        return {"probed": False, "why": "no probe address is registered for this credential"}
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host != (e.get("host") or "").lower():
        return {"probed": False, "findings": [finding("MAP-CRED-013", "BLOCK", f"the probe address is not on {e.get('host')}; it is not used")]}
    if (probe_host or "").strip().lower() != host:
        return {"probed": False, "needs_consent": True, "ask": f"send one small test request to {host} with your '{name}' key?", "then": f"call again with confirm_probe={host}"}
    cred = Resolved(name, e.get("kind", "apikey"), host, parse_inject(e.get("inject")), _read_value(path), reject=effective_reject(name, e.get("reject"), None, store), store=store)
    http = http or oh.SafeHTTP(limits={"timeout_s": 20, "max_bytes": 1_000_000, "retries": 0})
    try:
        http.get(url, expect="any", auth=cred)
        return {"probed": True, "accepted": True, "request": oh.redact_url(url) + f" (+ {_inject_shown(cred.inject)})"}
    except oh.ServiceError as ex:
        if getattr(ex, "rejected", False):
            return {"probed": True, "accepted": False, "findings": [finding("MAP-CRED-008", "BLOCK", str(ex))]}
        return {"probed": True, "accepted": None, "findings": [finding(ex.rule or "MAP-SVC-013", "WARN", f"the test request did not get an answer that says yes or no: {ex}")]}
    finally:
        cred.close()


def _place_shown(t: dict) -> str:
    return f"{t['query']}=***" if "query" in t else f"{t['header']}: {t['prefix']} ***" if t.get("prefix") else f"{t['header']}: ***"


def inject_query_shown(inj: dict | None) -> str:
    """The query-string part a run card shows (a=***&b=***), empty when nothing is sent in the address."""
    inj = inj or {}
    places = [inj[r] for r in ("user", "key") if r in inj] or [inj]
    return "&".join(_place_shown(t) for t in places if "query" in t)


def _inject_shown(inj: dict | None) -> str:
    inj = inj or {}
    if "postgres" in inj:
        p = inj["postgres"]
        return f"database {p['database']} as {p['user']} (port {p['port']}, sslmode {p['sslmode']}), password: ***"
    if "user" in inj:
        return "; ".join(_place_shown(inj[r]) for r in ("user", "key"))
    if "query" in inj or "header" in inj:
        return _place_shown(inj)
    if inj.get("path"):
        return "in the address path at {key}: ***"
    return "Authorization: ***"


def check(store: Store, name: str, *, probe: bool = False, probe_host: str = "", http=None, fix: bool = False, which: str = "file") -> dict:
    """The yes/no file checks (and, with consent, one probe). Never the content. Sets status ready, or keeps rejected while the file still holds
    the key the service refused."""
    e = store.get(name)
    if e is None:
        return {"ok": False, "findings": [not_registered(name, store)]}
    path = store.path_of(e, which)
    if path is None:
        return {"ok": False, "findings": [finding("MAP-CRED-002", "BLOCK", f"'{name}' has no {which.replace('_', ' ')} registered")]}
    if fix and path.exists():
        _try(lambda: store.fix_privacy(path.parent, True), False)
        _try(lambda: store.fix_privacy(path), False)
    a = store.check_file(path, e.get("kind", "apikey"), e.get("shape"), e.get("host"))
    fs = store.file_findings(name, path, a, e.get("kind", "apikey"))
    answers = {k: a[k] for k in ("exists", "regular", "private", "folder_private", "shaped", "synced", "inside", "mount_synthetic", "privacy_how")}
    answers["private"] = "not verifiable here" if a["private"] is None and a["mount_synthetic"] else a["private"]
    out = {"ok": False, "name": name, "host": e.get("host"), "file": _shown(path, store.home), "answers": answers, "findings": fs}
    if which != "file":
        out["ok"] = not blocks(fs)
        return out
    if blocks(fs):
        store.update(name, status="missing" if not a["exists"] else e.get("status") if e.get("status") == "rejected" else "unchecked", last_checked=today().isoformat())
        return out
    if e.get("status") == "rejected":
        rj = e.get("rejected_on")
        try:
            changed = bool(rj) and dt.datetime.fromtimestamp(os.stat(path).st_mtime) > dt.datetime.fromisoformat(str(rj))
        except (OSError, ValueError):
            changed = False
        if not changed:
            # 1.0.2 (F2): the probe is the plugin's own proof that a key works, so a consented probe may clear a rejection the file never caused
            purl = probe_url(store, name, e)
            phost = (urllib.parse.urlsplit(purl).hostname or "").lower() if purl else ""
            other = bool(purl and e.get("rejected_url") and urllib.parse.urlsplit(str(e["rejected_url"])).path.rstrip("/")
                         != urllib.parse.urlsplit(shown_url(purl, {"path": True})).path.rstrip("/"))
            told = (f"the file still holds the key the service rejected ({rejection_text(e)}"
                    + (", not by the test address" if other else "") + "); put a new key in it (or run "
                    f"/{_prefix()}:map-credentials rotate {name})"
                    + (f", or, if you believe the key is fine, run /{_prefix()}:map-credentials check {name} --probe to test it once against {phost}" if purl else "")
                    + ", then check again")
            if not (probe and purl):
                fs.append(finding("MAP-CRED-008", "BLOCK", told))
                store.update(name, last_checked=today().isoformat())
                return out
            before = dict(e)
            pr = _probe(store, name, e, path, probe_host, http)
            out["probe"] = {k: v for k, v in pr.items() if k != "findings"}
            fs += pr.get("findings", [])
            if not pr.get("accepted"):
                if pr.get("accepted") is None:            # no consent yet, or no yes-or-no answer: the rejection stands
                    fs.append(finding("MAP-CRED-008", "BLOCK", told))
                store.update(name, last_checked=today().isoformat())
                return out
            fs.append(finding("MAP-CRED-008", "INFO", f"the key was accepted by the test address; the earlier rejection ({rejection_text(before)}) was not the key"))
            store.update(name, last_probe=today().isoformat(), status="ready", last_checked=today().isoformat(), rejected_on=None, rejected_why=None,
                         rejected_url=None, rejected_via=None)
            out.update(ok=not blocks(fs), status="ready", findings=fs + _review_warnings(name, store.get(name)))
            return out
    new_status = "ready"
    if probe:
        pr = _probe(store, name, e, path, probe_host, http)
        out["probe"] = {k: v for k, v in pr.items() if k != "findings"}
        fs += pr.get("findings", [])
        if pr.get("needs_consent"):
            new_status = e.get("status") if e.get("status") in ("ready",) else "unchecked"
        elif pr.get("accepted") is False:
            out["findings"] = fs
            return out                                    # marked rejected by the request itself
        elif pr.get("accepted"):
            store.update(name, last_probe=today().isoformat())
    store.update(name, status=new_status, last_checked=today().isoformat(), rejected_on=None, rejected_why=None, rejected_url=None, rejected_via=None)
    out.update(ok=not blocks(fs), status=new_status, findings=fs + _review_warnings(name, store.get(name)))
    return out


def rotate(store: Store, name: str, *, finish: bool = False, probe: bool = False, probe_host: str = "", http=None) -> dict:
    """start: an empty private NAME.next.key; finish: check it (probe with consent), switch the registry to it, keep the old file as previous_file."""
    e = store.get(name)
    if e is None:
        return {"ok": False, "findings": [not_registered(name, store)]}
    if not finish:
        cur = e.get("file")
        taken = {cur, e.get("previous_file")}
        nxt = e.get("next_file")
        if not nxt:
            i = 1
            while True:
                nxt = f"{name}.next.key" if i == 1 else f"{name}.next-{i}.key"
                if nxt not in taken and not (store.root / nxt).exists():
                    break
                i += 1
        p = store.root / nxt
        fs = _create_private(store, p)
        store.update(name, next_file=nxt)
        return {"ok": not blocks(fs), "name": name, "step": "start", "next_file": _shown(p, store.home), "findings": fs,
                "next": ["Revoke or regenerate the key at the service first if you think it leaked.",
                         f"Open {str(p) if store.plat.windows else _shown(p, store.home)} in your editor, paste the NEW {'username:token' if e.get('kind') == 'userkey' else 'key'} as the only line, and save.",
                         f"Then run /{_prefix()}:map-credentials rotate {name} --finish."]}
    if not e.get("next_file"):
        return {"ok": False, "findings": [finding("MAP-CRED-009", "BLOCK", f"no rotation is under way for '{name}'; run map-credentials rotate {name} first")]}
    c = check(store, name, which="next_file")
    fs = list(c["findings"])
    if blocks(fs):
        return {"ok": False, "name": name, "step": "finish", "answers": c.get("answers"), "findings": fs}
    p = store.path_of(e, "next_file")
    if probe:
        pr = _probe(store, name, {**e, "status": "unchecked"}, p, probe_host, http)
        fs += pr.get("findings", [])
        if pr.get("accepted") is False or pr.get("needs_consent"):
            return {"ok": False, "name": name, "step": "finish", "probe": {k: v for k, v in pr.items() if k != "findings"}, "findings": fs}
    old = e.get("file")
    store.update(name, file=e["next_file"], next_file=None, previous_file=old, rotated=today().isoformat(), status="ready", last_checked=today().isoformat(),
                 rejected_on=None, rejected_why=None, review_after=(today() + dt.timedelta(days=365)).isoformat(), expires=None)
    oldp = store.root / old if old else None
    return {"ok": True, "name": name, "step": "finish", "status": "ready", "now_using": _shown(p, store.home), "findings": fs,
            "old_file": _shown(oldp, store.home) if oldp else None,
            "next": [f"Delete the old key file {_shown(oldp, store.home) if oldp else ''} yourself once you are sure the new key works; status reminds you after {OLD_FILE_DAYS} days."]
                    + ([f"The old token's end date ({e['expires']}) was cleared; run map-credentials add {name} --expires DATE with the new token's end date."] if e.get("expires") else []) + [
                     f"If the old key may have leaked, run /{_prefix()}:map-credentials audit to look for it in your working folder."]}


def remove(store: Store, name: str, *, delete_files: bool = False, services_dir: Path | None = None) -> dict:
    """Drop the registry entry and every profile reference to cred:NAME; list the files for the person to delete (deleted only when asked)."""
    import yaml
    reg = store.load()
    e = reg["credentials"].pop(name, None)
    if e is None:
        return {"ok": False, "findings": [not_registered(name, store)]}
    store.save(reg)
    profiles = []
    sd = services_dir or (gc.workdir() / "services")
    for p in sorted(sd.glob("*.yaml")) if sd.is_dir() else []:
        try:
            y = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        au = y.get("auth") or {}
        if au.get("credentials") == f"cred:{name}":
            au.pop("credentials", None)
            y["auth"] = au
            p.write_text(yaml.safe_dump(y, sort_keys=False), encoding="utf-8")
            profiles.append(y.get("profile") or p.stem)
    files = [store.path_of(e, k) for k in ("file", "next_file", "previous_file")]
    files = [f for f in files if f and f.exists()]
    deleted, left = [], []
    for f in files:
        if delete_files:
            try:
                f.unlink()
                deleted.append(_shown(f, store.home))
                continue
            except OSError:
                pass
        left.append(_shown(f, store.home))
    return {"ok": True, "name": name, "profiles_updated": profiles, "deleted": deleted, "files_to_delete": left,
            "next": ([f"Delete {', '.join(left)} yourself (the plugin deletes nothing unless you ask)."] if left else [])
            + (["The profiles listed no longer name a credential; add one again before using them."] if profiles else [])}


# ---------------------------------------------------------------------------------------------------------------- scanning (audit and release gate)
_HEX40 = r"[0-9a-fA-F]{40}"
# (rule, regex, group holding the value or 0 for the whole match). Each value is checked against PLACEHOLDER before it counts.
SCAN_RULES = [
    ("address key", re.compile(r"(?i)[?&;](?:key|api_?key|api-key|token|access_token|auth|authkey|sig|signature|client_secret|password|passwd|pwd|subscription-key)=([^&\s\"'<>#`\\]+)"), 1),
    ("assigned secret", re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|secret[_-]?key|password|passwd)[\"']?\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']"), 1),
    ("authorization header", re.compile(r"(?i)\bauthorization[\"']?\s*[:=]\s*[\"']?(?:basic|bearer|token|apikey)\s+([A-Za-z0-9._~+/=-]{8,})"), 1),
    ("api key header", re.compile(r"(?i)\b(?:x-api-key|x-auth-token|ocp-apim-subscription-key)[\"']?\s*[:=]\s*[\"']?([^\s\"',;]{8,})"), 1),
    ("password in an address", re.compile(r"://[^/\s:@\"'<>]+:([^/\s@\"'<>]{3,})@[A-Za-z0-9]"), 1),
    ("Census key", re.compile(r"(?i)(?:\b(?:api[_-]?key|census[_-]?(?:api[_-]?)?key|key)\s*[\"']?\s*[=:]\s*[\"']?|census[^\n]{0,30}?\bkey\b[^\n0-9a-f]{0,12})(" + _HEX40 + r")(?![0-9a-fA-F])"), 1),
    ("Mapbox token", re.compile(r"\b[sp]k\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"), 0),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"), 0),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), 0),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE" + r" KEY-----"), 0),
]
PLACEHOLDER = re.compile(r"""(?ix)^(?:
      .*\*\*\*.* | [{$<%\[(].* | .*[{}()].* | x{3,}.* | \.\.\.+ | .*…
    | (?:your|my|insert|enter|put)[_ -]?.* | (?:the|a|an|some)[_ -].* | example.* | .*example | placeholder.* | changeme | redacted | dummy.* | sample.* | test[-_]?(?:key|token)?
    | none | null | nil | true | false | undefined | string | value | secret | password | token | key | apikey | xxxx.* | 0+ | 1234.* | abc | abcdef
)$""")
NAME_RULES = (("credential file", re.compile(r"(?i)(?:^|/)[^/]+\.key$")), ("netrc file", re.compile(r"(?i)(?:^|/)(?:\.netrc|[^/]*\.netrc|_netrc)$")),
              ("working folder", re.compile(r"(?:^|/)\.geo-mapping/")))
DATA_URI = re.compile(r"data:[^,\s\"']{0,80};base64,[A-Za-z0-9+/=]+")
B64_RUN = re.compile(r"[A-Za-z0-9+/=]{160,}")
ZIP_SUFFIXES = (".zip", ".kmz", ".plugin", ".docx", ".xlsx", ".pptx", ".3mf")
MAX_SCAN_BYTES = 64 * 1024 * 1024


def _value_counts(rule: str, v: str) -> bool:
    if rule in ("Mapbox token", "AWS access key", "GitHub token", "Google API key", "private key", "Census key"):
        return True
    return not PLACEHOLDER.match(v.strip("\"'`;,.")) and len(set(v)) > 2


def scan_text(text: str, values: dict | None = None) -> list[tuple[int, str]]:
    """(line number, rule) for every secret pattern in a text, and for every exact value given ({name: value}: the value is never returned)."""
    out: list[tuple[int, str]] = []
    vals = [(n, forms) for n, v in (values or {}).items() if v and len(v) >= redact.MIN_MASK_LEN for forms in [redact._forms(v)]]
    for i, line in enumerate(text.split("\n"), 1):
        if len(line) > 20000:
            line = line[:20000]
        clean = B64_RUN.sub(" ", DATA_URI.sub(" ", line))
        for rule, rx, grp in SCAN_RULES:
            for m in rx.finditer(clean):
                v = m.group(grp) if grp else m.group(0)
                if _value_counts(rule, v):
                    out.append((i, rule))
                    break
        for n, forms in vals:
            if any(f in line for f in forms):
                out.append((i, f"the value of credential '{n}'"))
    return out


def _texts(path: Path):
    """(label, text) pieces of a file worth scanning: plain text, zip members, PNG text chunks, inflated PDF streams. Binary data is skipped."""
    import zlib
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size > MAX_SCAN_BYTES:
        return
    low = path.name.lower()
    if low.endswith(ZIP_SUFFIXES):
        import zipfile
        try:
            with zipfile.ZipFile(path) as z:
                for info in z.infolist():
                    if info.is_dir() or info.file_size > MAX_SCAN_BYTES:
                        continue
                    for lbl in NAME_RULES:
                        if lbl[1].search(info.filename):
                            yield f"{info.filename}", f"\x00name:{lbl[0]}"
                    data = z.read(info)
                    if b"\x00" not in data[:8192]:
                        yield info.filename, data.decode("utf-8", errors="replace")
            return
        except (zipfile.BadZipFile, OSError):
            pass
    data = path.read_bytes()
    if data.startswith(b"\x89PNG"):
        i = 8
        while i + 8 <= len(data):
            n = int.from_bytes(data[i:i + 4], "big")
            typ = data[i + 4:i + 8]
            body = data[i + 8:i + 8 + n]
            if typ == b"tEXt":
                yield "PNG text", body.decode("latin-1", errors="replace")
            elif typ == b"zTXt":
                k, _, rest = body.partition(b"\x00")
                yield "PNG text", (k + b" " + _try(lambda: zlib.decompress(rest[1:]), b"")).decode("latin-1", errors="replace")
            elif typ == b"iTXt":
                parts = body.split(b"\x00", 2)
                if len(parts) == 3 and len(parts[2]) > 2:
                    comp, rest = parts[1][:1], parts[2][2:]
                    rest = rest.split(b"\x00", 2)[-1] if b"\x00" in rest else rest
                    txt = _try(lambda: zlib.decompress(rest), rest) if comp == b"\x01" else rest
                    yield "PNG text", (parts[0] + b" " + txt).decode("utf-8", errors="replace")
            i += 12 + n
        return
    if data.startswith(b"%PDF"):
        yield "PDF", data.decode("latin-1", errors="replace")
        for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
            raw = _try(lambda: zlib.decompress(m.group(1)), None)
            if raw and b"\x00" not in raw[:4096]:
                yield "PDF stream", raw.decode("latin-1", errors="replace")
        return
    if b"\x00" in data[:8192]:
        return
    yield "", data.decode("utf-8", errors="replace")


@dataclass
class AllowRule:
    path: str
    line: str
    reason: str

    def allows(self, rel: str, text: str) -> bool:
        return fnmatch.fnmatch(rel, self.path) and (not self.line or re.search(self.line, text) is not None)


def load_allow(path: Path | None) -> list[AllowRule]:
    if not path or not Path(path).is_file():
        return []
    import yaml
    y = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out = []
    for r in y.get("allow") or []:
        if not r.get("path") or not r.get("reason"):
            continue
        out.append(AllowRule(str(r["path"]), str(r.get("line") or ""), str(r["reason"])))
    return out


def scan_files(root: Path, rels: list[str], *, allow: list[AllowRule] | None = None, values: dict | None = None, label: str | None = None,
               skip_rules: tuple = ()) -> list[dict]:
    """Hits as {file, line, rule}; never the matched text. `rels` are paths under root (POSIX form)."""
    allow = allow or []
    hits: list[dict] = []
    root = Path(root)
    for rel in rels:
        p = root / rel
        shown = f"{label}/{rel}" if label else rel
        for rule, rx in NAME_RULES:
            if rule not in skip_rules and rx.search(rel) and not any(a.allows(rel, "") for a in allow):
                hits.append({"file": shown, "line": 0, "rule": rule})
        if p.name == REGISTRY:
            hits += _registry_hit(p, rel, shown, allow)
        if not p.is_file():
            continue
        try:
            pieces = list(_texts(p))
        except OSError:
            continue
        for member, text in pieces:
            where = f"{shown}!{member}" if member and member not in ("PNG text", "PDF", "PDF stream") else shown
            if text.startswith("\x00name:"):
                if not any(a.allows(f"{rel}!{member}", "") for a in allow):
                    hits.append({"file": where, "line": 0, "rule": text[6:]})
                continue
            lines = text.split("\n")
            for ln, rule in scan_text(text, values):
                src = lines[ln - 1] if 0 < ln <= len(lines) else ""
                # 0.23.0: inside a zip (a .plugin package) the allow rules match the member's own path too (hooks/README.md), not
                # only the zip's file name, so a fixture allowed in the edition folder is allowed in its package
                if any(a.allows(rel, src) or (member and where != shown and a.allows(member, src)) for a in allow):
                    continue
                hits.append({"file": where, "line": ln, "rule": rule})
    return hits


def _registry_hit(p: Path, rel: str, shown: str, allow) -> list[dict]:
    """A credentials.yaml with a non-empty file: that exists beside it is a real credential folder."""
    try:
        import yaml
        y = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return []
    for e in (y.get("credentials") or {}).values():
        f = (e or {}).get("file") if isinstance(e, dict) else None
        if f and (p.parent / f).is_file() and not any(a.allows(rel, "") for a in allow):
            return [{"file": shown, "line": 0, "rule": "credential registry with its files"}]
    return []


def walk(root: Path, skip: list[Path] | None = None) -> list[str]:
    root = Path(root)
    out = []
    skip = [Path(s).resolve() for s in (skip or [])]
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        dirnames[:] = [x for x in dirnames if x not in ("__pycache__", ".git") and not any(_under(d / x, s) for s in skip)]
        for f in filenames:
            out.append((d / f).relative_to(root).as_posix())
    return sorted(out)


def audit(store: Store, folder: Path | None = None, *, with_values: bool = True) -> dict:
    """MAP-CRED-006: the working folder (default) scanned for secret patterns and, where the credential folder is here, for the exact value of every
    registered key (current, next and previous files). Hits by file and line, never the value."""
    explicit = folder is not None
    folder = Path(folder) if folder else gc.workdir()
    if not explicit and not folder.is_dir():
        return {"ok": True, "folder": str(folder), "files_scanned": 0, "exact_values_checked": [], "hits": [], "findings": [],
                "next": ["There is no working folder here yet, so there is nothing to audit."]}
    values: dict = {}
    if with_values and store.exists():
        for n, e in store.load()["credentials"].items():
            for which in ("file", "next_file", "previous_file"):
                p = store.path_of(e, which)
                if p and p.is_file():
                    v = _try(lambda: _read_value(p), "")
                    if v and e.get("kind") == "userkey":
                        v = v.partition(":")[2]            # the token is the secret; the username is only an identifier
                    if v:
                        note_value(v)
                        values[n if which == "file" else f"{n} ({which.replace('_', ' ')})"] = v
    if not folder.is_dir():
        return {"ok": False, "findings": [finding("MAP-CRED-006", "BLOCK", f"no folder {folder} to audit")]}
    rels = walk(folder, skip=[store.root])
    hits = scan_files(folder, rels, values=values, skip_rules=("working folder",))
    fs = [finding("MAP-CRED-006", "BLOCK", f"{h['file']} line {h['line']}: {h['rule']}", {"file": h["file"], "line": h["line"]}) for h in hits]
    return {"ok": not hits, "folder": str(folder), "files_scanned": len(rels), "exact_values_checked": sorted(values), "hits": hits, "findings": fs,
            "next": ["Remove each hit from the file. If a real key was written anywhere, treat it as exposed: revoke it at the service and rotate."] if hits else []}


# ---------------------------------------------------------------------------------------------------------------- pasted keys (MAP-CRED-007)
def looks_pasted(text: str) -> list[str]:
    """The kinds of secret in a message; the skill uses it to say: not used, treat it as exposed, rotate."""
    kinds = redact.find(text or "")
    for _ln, rule in scan_text(text or ""):
        if rule not in kinds:
            kinds.append(rule)
    if re.search(r"(?<![0-9a-fA-F])" + _HEX40 + r"(?![0-9a-fA-F])", text or "") and "Census key" not in kinds and re.search(r"(?i)census|key", text or ""):
        kinds.append("a 40-character hexadecimal key")
    return [k for k in kinds if not any(k != o and k in o for o in kinds)]     # 0.15.0: "password in an address" inside "a user name and password in an address"



def pasted_finding(text: str) -> dict | None:
    """MAP-CRED-007 for a message that holds a key, token or password: it is not used, never repeated, and the person is told to treat it
    as exposed. Used by the optional UserPromptSubmit hook (hooks/credentials_reminder.py) and by the skill's own check. Never quotes it."""
    kinds = looks_pasted(text)
    if not kinds:
        return None
    k = kinds[0] if len(kinds) == 1 else ", ".join(kinds[:-1]) + " and " + kinds[-1]
    return finding("MAP-CRED-007", "BLOCK", f"the message appears to hold {k}; it is not used or stored. Keys go in the person's own credential "
                   "folder (map-credentials add NAME); a key typed into the chat should be treated as exposed: revoke it and rotate", kinds)

# ---------------------------------------------------------------------------------------------------------------- summary for map-doctor
def doctor_summary(store: Store | None = None, mcp: str = "unknown") -> dict:
    store = store or Store()
    s = status(store)
    rows = s["credentials"]
    return {"count": s["count"], "folder_found": s["folder_found"], "mcp_server": mcp,
            "rejected": [r["name"] for r in rows if r["status"] == "rejected"], "expired": [r["name"] for r in rows if r["status"] == "expired"],
            "missing": [r["name"] for r in rows if r["status"] == "missing"], "unverifiable": [r["name"] for r in rows if r["private"] == "not verifiable here"],
            "not_private": [r["name"] for r in rows if r["private"] == "no"], "old_file_pending": [r["name"] for r in rows if r["old_file_pending"]],
            "browser_unrestricted": [r["name"] for r in rows if r["class"] == "browser" and not r["restricted"]]}


# ---------------------------------------------------------------------------------------------------------------- CLI (the fallback route)
def _exit(res: dict) -> int:
    if res.get("ok"):
        return gc.EXIT_OK
    ids = [f["id"] for f in blocks(res.get("findings") or [])]
    if "MAP-CRED-008" in ids:
        return gc.EXIT_SERVICE
    return gc.EXIT_LINT if ids else gc.EXIT_USAGE


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", help="the credential folder (the fallback: the folder you connected to this session)")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="op", required=True)
    sub.add_parser("status")
    p = sub.add_parser("add")
    p.add_argument("name")
    p.add_argument("--class", dest="cls", choices=CLASSES)
    p.add_argument("--confirm-restricted", action="store_true", help="you have restricted this browser key to your domain in the provider's dashboard")
    p.add_argument("--host")
    p.add_argument("--inject", help="query:NAME, header:NAME, header:NAME:PREFIX (sent as 'PREFIX key'), bearer, basic, path (the key at {key} in the address path); "
                                    "for a username and a key: user=header:NAME,key=header:NAME (with --kind userkey)")
    p.add_argument("--kind", choices=KINDS)
    p.add_argument("--review-after")
    p.add_argument("--expires", help="the day the service says the token ends (YYYY-MM-DD); runs refuse it after that day")
    p = sub.add_parser("check")
    p.add_argument("name")
    p.add_argument("--probe", action="store_true")
    p.add_argument("--confirm-probe", default="")
    p.add_argument("--fix", action="store_true", help="apply the privacy rule to the file and its folder first")
    p = sub.add_parser("rotate")
    p.add_argument("name")
    p.add_argument("--finish", action="store_true")
    p.add_argument("--probe", action="store_true")
    p.add_argument("--confirm-probe", default="")
    p = sub.add_parser("remove")
    p.add_argument("name")
    p.add_argument("--delete-files", action="store_true")
    p = sub.add_parser("audit")
    p.add_argument("folder", nargs="?")
    p = sub.add_parser("scan")
    p.add_argument("paths", nargs="+")
    p.add_argument("--allow")
    a = ap.parse_args(argv)
    if a.op == "scan":
        allow = load_allow(Path(a.allow)) if a.allow else []
        hits = []
        for raw in a.paths:
            r = Path(raw)
            hits += scan_files(r, walk(r), allow=allow, label=r.name) if r.is_dir() else scan_files(r.parent, [r.name], allow=allow)
        res = {"ok": not hits, "hits": hits, "findings": [finding("MAP-CRED-010", "BLOCK", f"{h['file']}:{h['line']} {h['rule']}") for h in hits]}
        print(json.dumps(res, indent=2))
        return gc.EXIT_OK if not hits else gc.EXIT_LINT
    store = Store(a.dir)
    with value_scope() as vs:
        try:
            if a.op == "status":
                res = status(store)
            elif a.op == "add":
                res = add(store, a.name, cls=a.cls, restricted=a.confirm_restricted, host=a.host, inject=a.inject, kind=a.kind, review_after=a.review_after, expires=a.expires)
            elif a.op == "check":
                res = check(store, a.name, probe=a.probe, probe_host=a.confirm_probe, fix=a.fix)
            elif a.op == "rotate":
                res = rotate(store, a.name, finish=a.finish, probe=a.probe, probe_host=a.confirm_probe)
            elif a.op == "remove":
                res = remove(store, a.name, delete_files=a.delete_files)
            else:
                res = audit(store, Path(a.folder) if a.folder else None)
        except CredentialError as ex:
            res = {"ok": False, "findings": ex.findings}
        res = vs.clean(res)
    if a.dir:
        res.setdefault("notice", "The credential folder is connected to this session, so the session can read it. The plugin's MCP server on your computer avoids that.")
    print(json.dumps(res, indent=2, default=str))
    return _exit(res)


if __name__ == "__main__":
    sys.exit(main())
