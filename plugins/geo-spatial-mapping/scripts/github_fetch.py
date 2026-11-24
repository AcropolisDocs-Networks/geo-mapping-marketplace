#!/usr/bin/env python3
"""github_fetch: a file or a folder listing from a public GitHub repository, by whatever route answers (1.16.0, Day 14 issue 11).

    github_fetch.py get OWNER/REPO PATH [--ref master] --out FILE
    github_fetch.py ls  OWNER/REPO [DIR] [--ref master]

Data the library points at on GitHub (the Ancient World Mapping Center's geodata, historical-basemaps) was out of reach from the cloud
workspace on Day 14: the GitHub API answered 403, while a sparse git clone worked. Each call here tries, in order, and says which route it took:

  file     raw.githubusercontent.com -> media.githubusercontent.com (Git LFS content) -> cdn.jsdelivr.net/gh (GitHub's files through jsDelivr)
           -> a sparse, shallow git clone of the one folder (git must be installed)
  listing  api.github.com (contents) -> data.jsdelivr.com (the package's flat file list) -> a sparse git clone

Every HTTP request goes through the plugin's guarded client (ogc_http.SafeHTTP: public addresses only, size and time limits). A clone runs
`git clone --depth 1 --filter=blob:none --sparse` into a scratch folder that is removed afterwards. No credentials are sent.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

MAX_BYTES = 400 * 1024 * 1024
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubError(RuntimeError):
    def __init__(self, msg: str, code: int = gc.EXIT_RUNTIME):
        super().__init__(msg)
        self.code = code


def _http(http=None):
    if http is not None:
        return http
    import ogc_http
    return ogc_http.SafeHTTP(limits={"timeout_s": 120, "max_bytes": MAX_BYTES})


def _check(repo: str, path: str = "") -> tuple[str, str]:
    if not REPO.match(repo or ""):
        raise GitHubError(f"'{repo}' is not OWNER/REPO", gc.EXIT_USAGE)
    p = str(path or "").strip("/")
    if ".." in p.split("/"):
        raise GitHubError("the path may not climb out of the repository", gc.EXIT_USAGE)
    return repo, p


def file_urls(repo: str, path: str, ref: str = "master") -> list[tuple[str, str]]:
    """The HTTP routes to one file, in the order they are tried: [(route, url)]."""
    repo, path = _check(repo, path)
    return [("raw", f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"),
            ("lfs", f"https://media.githubusercontent.com/media/{repo}/{ref}/{path}"),
            ("jsdelivr", f"https://cdn.jsdelivr.net/gh/{repo}@{ref}/{path}")]


def _git_ok() -> bool:
    return shutil.which("git") is not None


def _clone(repo: str, ref: str, folder: str, work: Path) -> Path:
    """A shallow, sparse clone holding only `folder` (the whole tree's names, the blobs of that folder)."""
    dst = work / "repo"
    url = f"https://github.com/{repo}.git"
    cmds = [["git", "clone", "--quiet", "--depth", "1", "--filter=blob:none", "--sparse", "--branch", ref, url, str(dst)]]
    if folder:
        cmds.append(["git", "-C", str(dst), "sparse-checkout", "set", "--no-cone", folder])
    for c in cmds:
        r = subprocess.run(c, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise GitHubError(f"git {' '.join(c[1:3])} failed: {r.stderr.strip()[:200]}")
    return dst


def get(repo: str, path: str, *, ref: str = "master", http=None, clone_ok: bool = True) -> tuple[bytes, dict]:
    """One file's bytes and the record of how it came: {repo, path, ref, route, url, tried}."""
    repo, path = _check(repo, path)
    h = _http(http)
    tried = []
    for route, url in file_urls(repo, path, ref):
        try:
            r = h.get(url, max_bytes=MAX_BYTES)
        except Exception as ex:  # noqa: BLE001 - a refused host is tried on the next route
            tried.append(f"{route}: {type(ex).__name__}")
            continue
        if r.status == 200 and not r.content.startswith(b"version https://git-lfs"):
            return r.content, {"repo": repo, "path": path, "ref": ref, "route": route, "url": url, "tried": tried}
        tried.append(f"{route}: HTTP {r.status}" + (" (a Git LFS pointer)" if r.content.startswith(b"version https://git-lfs") else ""))
    if clone_ok and _git_ok():
        work = Path(tempfile.mkdtemp(prefix="gm-gh-"))
        try:
            d = _clone(repo, ref, str(Path(path).parent).replace("\\", "/").strip(".") or path, work)
            f = d / path
            if f.is_file():
                return f.read_bytes(), {"repo": repo, "path": path, "ref": ref, "route": "git-clone", "url": f"https://github.com/{repo}.git", "tried": tried}
            tried.append("git-clone: the file is not in the clone")
        except (GitHubError, subprocess.TimeoutExpired) as ex:
            tried.append(f"git-clone: {str(ex)[:120]}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
    raise GitHubError(f"{repo}/{path}@{ref} could not be read by any route (" + "; ".join(tried) + ")")


def ls(repo: str, folder: str = "", *, ref: str = "master", http=None, clone_ok: bool = True) -> tuple[list[str], dict]:
    """The files under `folder` (paths from the repository root) and how the list came."""
    repo, folder = _check(repo, folder)
    h = _http(http)
    tried = []
    try:
        r = h.get(f"https://api.github.com/repos/{repo}/contents/{folder}", params={"ref": ref}, max_bytes=20_000_000)
        if r.status == 200:
            items = json.loads(r.content)
            return sorted(i["path"] for i in items if i.get("type") == "file"), {"route": "api", "tried": tried}
        tried.append(f"api: HTTP {r.status}")
    except Exception as ex:  # noqa: BLE001
        tried.append(f"api: {type(ex).__name__}")
    try:
        r = h.get(f"https://data.jsdelivr.com/v1/packages/gh/{repo}@{ref}", params={"structure": "flat"}, max_bytes=50_000_000)
        if r.status == 200:
            files = [f["name"].lstrip("/") for f in json.loads(r.content).get("files") or []]
            pre = (folder + "/") if folder else ""
            return sorted(f for f in files if f.startswith(pre)), {"route": "jsdelivr", "tried": tried}
        tried.append(f"jsdelivr: HTTP {r.status}")
    except Exception as ex:  # noqa: BLE001
        tried.append(f"jsdelivr: {type(ex).__name__}")
    if clone_ok and _git_ok():
        work = Path(tempfile.mkdtemp(prefix="gm-gh-"))
        try:
            d = _clone(repo, ref, folder, work)
            base = d / folder if folder else d
            files = [str(p.relative_to(d)).replace("\\", "/") for p in base.rglob("*") if p.is_file() and ".git" not in p.parts]
            return sorted(files), {"route": "git-clone", "tried": tried}
        except (GitHubError, subprocess.TimeoutExpired) as ex:
            tried.append(f"git-clone: {str(ex)[:120]}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
    raise GitHubError(f"the files of {repo}/{folder}@{ref} could not be listed (" + "; ".join(tried) + ")")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get")
    g.add_argument("repo")
    g.add_argument("path")
    g.add_argument("--ref", default="master")
    g.add_argument("--out", required=True)
    g.add_argument("--no-clone", action="store_true")
    s = sub.add_parser("ls")
    s.add_argument("repo")
    s.add_argument("folder", nargs="?", default="")
    s.add_argument("--ref", default="master")
    s.add_argument("--no-clone", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "get":
            data, rec = get(a.repo, a.path, ref=a.ref, clone_ok=not a.no_clone)
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out).write_bytes(data)
            print(json.dumps({**rec, "out": a.out, "bytes": len(data)}, indent=2))
        else:
            files, rec = ls(a.repo, a.folder, ref=a.ref, clone_ok=not a.no_clone)
            print(json.dumps({"files": files, **rec}, indent=2))
    except GitHubError as ex:
        print(f"github_fetch: {ex}", file=sys.stderr)
        return ex.code
    return gc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
