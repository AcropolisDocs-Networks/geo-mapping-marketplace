#!/usr/bin/env python3
"""credential_broker: HTTP Basic credentials by reference, for approved runs on the person's computer (spec 6.4, 6.7; M9).

The plugin never holds, reads, prints or stores a user name or password. A profile or a run names a *reference* to a secrets file that lives on the
person's computer, and the shell on that computer expands it at run time:

    netrc:PATH   a netrc file ("machine HOST login USER password PASS", words separated by spaces, no quoting); curl reads it with --netrc-file, a kit with its own reader
    file:PATH    a file whose one line is "user:password"; curl gets it through a config on stdin (-K -), a kit through its own environment

`env:NAME` is not accepted (the device shell does not see the person's environment, H.3 D3). A reference must be a plain path, the file must be private
(no group or world access), and it must sit outside the output folder and the project folder. What the plugin learns about the file is a handful of
yes/no answers printed by a check run in the device shell; the check never prints the file's content.

0.6.0 (credentials spec §7.2): API keys and bearer tokens are registered credentials, `cred:NAME` (and `apikey:PATH`), held in the person's
credential folder and read by `scripts/credentials.py` (host-side in the MCP server, or in-process with the folder connected). `parse_any` takes
every reference form. The device-shell check gains three answers: `synced` (the file sits in a cloud-synced folder, MAP-CRED-004 WARN),
`inside_project` (inside a folder passed after the host, MAP-CRED-003) and `mount_synthetic` (the folder is a mount whose permissions are made up,
such as a connected Windows folder: privacy is then not verifiable here, MAP-CRED-012 WARN, and never reported as private). Basic paths are unchanged.

    credential_broker.py check-command --ref REF --host HOST [--out-dir D] [--project-dir D]    the device command that checks the file
    credential_broker.py check-result  --ref REF --output FILE|-                                  read its answers (MAP-SVC-009, 029)
    credential_broker.py parse --ref REF                                                          what kind of reference this is (no file access)
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

PATH_RE = re.compile(r"^(?:\$HOME|~)?/[A-Za-z0-9._/@+=-]+$")
KINDS = ("netrc", "file")
CRED_KINDS = ("basic", "apikey", "bearer", "userkey")            # 0.6.0: the kinds a registered credential has (scripts/credentials.py)
SYNTHETIC = ("9p", "virtiofs", "drvfs", "vboxsf", "vmhgfs", "prl_fs", "cifs", "smb3", "smbfs", "ntfs", "ntfs3", "vfat", "msdos", "exfat")
SYNCED_PARTS = ("/onedrive", "/dropbox/", "/icloud", "/my drive/", "/mobile documents/", "/library/cloudstorage/", "/google drive/")

# The file check. Prints booleans only. argv: kind path host [folder ...]; the folders are the output and project folders the file must not be in.
CHECK_CODE = (
    "import os,sys,json,stat\n"
    "k,p,h=sys.argv[1],os.path.expanduser(os.path.expandvars(sys.argv[2])),sys.argv[3]\n"
    "r={'exists':False,'regular':False,'private':False,'shaped':False,'synced':False,'inside_project':False,'mount_synthetic':False,'error':None}\n"
    "rp=os.path.realpath(p)\n"
    "for d in sys.argv[4:]:\n"
    " d=os.path.realpath(os.path.expanduser(os.path.expandvars(d)))\n"
    " if rp==d or rp.startswith(d.rstrip('/')+'/'):\n"
    "  r['inside_project']=True\n"
    f"r['synced']=any(s in rp.lower()+'/' for s in {SYNCED_PARTS!r})\n"
    "try:\n"
    " best,ft='',''\n"
    " for ln in open('/proc/mounts'):\n"
    "  q=ln.split()\n"
    "  if len(q)>2:\n"
    "   m=q[1].replace('\\\\040',' ')\n"
    "   if (rp==m or rp.startswith(m.rstrip('/')+'/')) and len(m)>len(best):\n"
    "    best,ft=m,q[2]\n"
    f" r['mount_synthetic']=ft.startswith('fuse') or ft in {SYNTHETIC!r}\n"
    "except OSError:\n"
    " pass\n"
    "def nrc(t,h):\n"
    " w,cur,f,i=t.split(),None,{},0\n"
    " while i<len(w):\n"
    "  if w[i]=='machine' and i+1<len(w):\n"
    "   cur=w[i+1]\n"
    "   f.setdefault(cur,{})\n"
    "   i+=2\n"
    "  elif w[i]=='default':\n"
    "   cur='default'\n"
    "   f.setdefault(cur,{})\n"
    "   i+=1\n"
    "  elif w[i] in ('login','password') and cur and i+1<len(w):\n"
    "   f[cur][w[i]]=w[i+1]\n"
    "   i+=2\n"
    "  else:\n"
    "   i+=1\n"
    " e=f.get(h) or f.get('default') or {}\n"
    " return bool(e.get('login') and e.get('password'))\n"
    "try:\n"
    " st=os.stat(p)\n"
    " r['exists']=True\n"
    " r['regular']=stat.S_ISREG(st.st_mode)\n"
    " r['private']=(st.st_mode & 0o077)==0\n"
    " if r['regular']:\n"
    "  t=open(p,encoding='utf-8').read()\n"
    "  if k=='netrc':\n"
    "   r['shaped']=nrc(t,h)\n"
    "  else:\n"
    "   t=t.strip()\n"
    "   r['shaped']=(t.count(chr(10))==0 and ':' in t and bool(t.split(':',1)[0]) and bool(t.split(':',1)[1]))\n"
    "except Exception as e:\n"
    " r['error']=type(e).__name__\n"
    "print(json.dumps(r))\n")

# Turns a "user:password" file into a curl config line on stdout, escaped for curl's config syntax; the password is never in an argument list.
CONFIG_CODE = "import sys,json;print('user = '+json.dumps(open(sys.argv[1]).read().strip(),ensure_ascii=False))"


def finding(fid: str, sev: str, message: str, detail=None) -> dict:
    return {"id": fid, "severity": sev, "message": message, "detail": detail}


def parse_ref(ref: str) -> tuple[dict | None, list[dict]]:
    """A reference as {kind, path}. No file is touched. Refuses env:, inline values and anything that is not a plain path (MAP-SVC-029)."""
    fs: list[dict] = []
    ref = (ref or "").strip()
    kind, sep, path = ref.partition(":")
    if not sep or kind.lower() == "env":
        fs.append(finding("MAP-SVC-029", "BLOCK", "credential references are netrc:PATH or file:PATH (a secrets file on your computer); env: is not available because the device shell does not see your environment"
                          if kind.lower() == "env" else "a credential reference looks like netrc:PATH or file:PATH; a user name or password is never given here"))
        return None, fs
    kind = kind.lower()
    if kind in ("cred", "apikey"):
        fs.append(finding("MAP-SVC-029", "BLOCK", f"'{kind}:' names an API key, which the plugin's MCP server uses on your computer through a profile with "
                                                  "auth: {method: credential, credentials: cred:NAME}; an approved run takes a login file (netrc: or file:)"))
        return None, fs
    if kind not in KINDS:
        fs.append(finding("MAP-SVC-029", "BLOCK", f"'{kind}:' is not a credential reference this plugin accepts (netrc: or file:)"))
        return None, fs
    path = path.strip()
    if not PATH_RE.match(path) or ".." in path.split("/") or path.endswith("/"):
        fs.append(finding("MAP-SVC-029", "BLOCK", "the secrets file must be given as a plain path on your computer (letters, digits and . _ - / @ + = only, starting with / or $HOME/); nothing else is accepted"))
        return None, fs
    return {"kind": kind, "path": path}, fs


def _norm(p: str) -> str:
    p = p.rstrip("/")
    return "$HOME" + p[1:] if p.startswith("~") else p


def outside(path: str, dirs: list[str]) -> list[str]:
    """The folders (output, project) that the secrets file is inside; the file must be in none of them."""
    n = _norm(path)
    return [d for d in dirs if d and (n == _norm(d) or n.startswith(_norm(d) + "/"))]


def validate_ref(ref: str, *, out_dir: str | None = None, project_dir: str | None = None, endpoint: str | None = None) -> tuple[dict | None, list[dict]]:
    """Everything that can be said about a reference without touching the file: its form, where it sits, and that the endpoint is https (MAP-SVC-016)."""
    r, fs = parse_ref(ref)
    if r is None:
        return None, fs
    inside = outside(r["path"], [out_dir or "", project_dir or ""])
    if inside:
        fs.append(finding("MAP-SVC-029", "BLOCK", "the secrets file sits inside the output or project folder, where it could be copied or published; keep it in a separate, private folder"))
    if endpoint and not endpoint.lower().startswith("https://"):
        fs.append(finding("MAP-SVC-016", "BLOCK", "credentials are sent only over https; this endpoint is not https"))
    return (None if any(f["severity"] == "BLOCK" for f in fs) else r), fs


def check_command(ref: dict, host: str, dirs: list[str] | None = None) -> dict:
    """The command that asks the device shell the file's yes/no questions. The host is for the netrc lookup only and is not printed. `dirs` are
    folders (output, project) the file must not sit in; they follow the host on the command line."""
    if not re.fullmatch(r"[A-Za-z0-9.:\[\]-]{1,253}", host or ""):
        raise ValueError("not a valid host name")
    extra = "".join(" " + shlex.quote(d) for d in (dirs or []) if d)
    return {"command": f"python3 -c {shlex.quote(CHECK_CODE)} {ref['kind']} {shlex.quote(ref['path'])} {shlex.quote(host)}{extra}", "timeout_ms": 20000,
            "prints": "yes/no answers about the file; never its content"}


def check_result(ref: dict, output: str) -> dict:
    """MAP-SVC-009 and 029 from what the check printed; MAP-CRED-003, 004 and 012 from the 0.6.0 answers."""
    fs: list[dict] = []
    try:
        line = [l for l in output.strip().splitlines() if l.strip().startswith("{")][-1]
        r = json.loads(line)
    except (IndexError, ValueError):
        return {"ok": False, "findings": [finding("MAP-SVC-029", "BLOCK", "the check printed nothing readable; the device shell may not have python3")], "answers": None}
    synthetic = bool(r.get("mount_synthetic"))
    if not r.get("exists"):
        fs.append(finding("MAP-SVC-029", "BLOCK", "the secrets file was not found from the device shell; check the path and that its folder is connected"))
    elif not r.get("regular"):
        fs.append(finding("MAP-SVC-029", "BLOCK", "the reference does not point to a plain file"))
    else:
        if synthetic:
            fs.append(finding("MAP-CRED-012", "WARN", "privacy cannot be verified from the device shell: the folder is a mount whose permissions are made up (every file shows mode 700). "
                                                    "On your computer, check that only you, SYSTEM and Administrators can read the file"))
        elif not r.get("private"):
            fs.append(finding("MAP-SVC-029", "BLOCK", "the secrets file can be read by other accounts; make it private (mode 600) and check again"))
        if not r.get("shaped"):
            fs.append(finding("MAP-SVC-029", "BLOCK", "the secrets file has no entry for this host" if ref["kind"] == "netrc" else "the secrets file should hold one line: user:password"))
    if r.get("inside_project"):
        fs.append(finding("MAP-CRED-003", "BLOCK", "the secrets file sits inside the output or project folder, where it could be copied or published; keep it in a separate, private folder"))
    if r.get("synced"):
        fs.append(finding("MAP-CRED-004", "WARN", "the secrets file sits in a cloud-synced folder, which copies it to other devices and to the provider; move it to a folder that is not synced"))
    if r.get("error") and not fs:
        fs.append(finding("MAP-SVC-029", "BLOCK", f"the secrets file could not be read ({r['error']})"))
    answers = {k: r.get(k) for k in ("exists", "regular", "private", "shaped", "synced", "inside_project", "mount_synthetic")}
    if synthetic:
        answers["private"] = "not verifiable here"
    return {"ok": not any(f["severity"] == "BLOCK" for f in fs), "findings": fs, "answers": answers}


def parse_any(ref: str) -> tuple[dict | None, list[dict]]:
    """Every reference form of 0.6.0 (cred:NAME, apikey:PATH, netrc:PATH, file:PATH), MAP-CRED-001; the Basic forms keep MAP-SVC-029 in parse_ref."""
    import credentials as cr
    return cr.parse_ref(ref)


# ---------------------------------------------------------------------------------------------------------------------- command pieces
def curl_pieces(ref: dict) -> tuple[list[str], str]:
    """(extra curl arguments, a shell prefix that feeds curl). `netrc` adds --netrc-file PATH and nothing else; `file` pipes a config to `curl -K -`.
    The path is the only thing in either; the secret is read by the shell at run time."""
    if ref["kind"] == "netrc":
        return ["--netrc-file", ref["path"]], ""
    return ["-K", "-"], f"python3 -c {shlex.quote(CONFIG_CODE)} {shlex.quote(ref['path'])} | "


def kit_env_prefix(ref: dict) -> str:
    """Shell text, ending in `&&`, that gives the kit its credentials in its own environment: GEO_NETRC (a path) or GEO_USER and GEO_PASS read by the shell's own `read`."""
    if ref["kind"] == "netrc":
        return f"export GEO_NETRC={shlex.quote(ref['path'])} && "
    return f"IFS=: read -r GEO_USER GEO_PASS < {shlex.quote(ref['path'])} && export GEO_USER GEO_PASS && "


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="op", required=True)
    p = sub.add_parser("parse")
    p.add_argument("--ref", required=True)
    p = sub.add_parser("check-command")
    p.add_argument("--ref", required=True)
    p.add_argument("--host", required=True)
    p.add_argument("--out-dir")
    p.add_argument("--project-dir")
    p = sub.add_parser("check-result")
    p.add_argument("--ref", required=True)
    p.add_argument("--output", required=True)
    a = ap.parse_args(argv)
    if a.op == "parse":
        r, fs = parse_ref(a.ref)
        print(json.dumps({"ok": r is not None, "ref": r, "findings": fs}, indent=2))
        return gc.EXIT_OK if r else gc.EXIT_LINT
    r, fs = validate_ref(a.ref, out_dir=getattr(a, "out_dir", None), project_dir=getattr(a, "project_dir", None))
    if r is None:
        print(json.dumps({"ok": False, "findings": fs}, indent=2))
        return gc.EXIT_LINT
    if a.op == "check-command":
        try:
            print(json.dumps(check_command(r, a.host), indent=2))
        except ValueError as ex:
            print(f"credential_broker: {ex}", file=sys.stderr)
            return gc.EXIT_USAGE
        return gc.EXIT_OK
    out = sys.stdin.read() if a.output == "-" else Path(a.output).read_text()
    res = check_result(r, out)
    print(json.dumps(res, indent=2))
    return gc.EXIT_OK if res["ok"] else gc.EXIT_LINT


if __name__ == "__main__":
    sys.exit(main())
