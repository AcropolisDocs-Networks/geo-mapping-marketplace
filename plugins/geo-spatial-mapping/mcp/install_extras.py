"""1.6.3: the optional extras of `setup.cmd all`, one package at a time, so one that has no wheel for this machine does not stop the rest;
then Playwright's Chromium (flythroughs, the PNG fallback, the web-map checks). Prints a summary; always exits 0 (the map packages are in
already, and the summary names anything missing). Usage:  python install_extras.py path\\to\\requirements-optional.txt"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


def packages(req: Path) -> list[str]:
    out = []
    for ln in req.read_text(encoding="utf-8").splitlines():
        spec = ln.split("#", 1)[0].strip()
        if spec and not spec.startswith("-"):
            out.append(spec)
    return out


def main(argv: list[str]) -> int:
    req = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1] / "requirements-optional.txt"
    ok, failed = [], []
    for spec in packages(req):
        print(f"  {spec} ...", flush=True)
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--progress-bar", "off", "-q", spec])
        (ok if r.returncode == 0 else failed).append(re.split(r"[<>=\[ ]", spec)[0])
    browser = "not installed (playwright is missing)"
    if "playwright" in ok:
        print("  Playwright's Chromium (about 150 MB, once) ...", flush=True)
        r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
        browser = "installed" if r.returncode == 0 else "failed: run  python -m playwright install chromium  again"
    print()
    print(f"Optional extras: {len(ok)} installed" + (f"; not installed: {', '.join(failed)} (the maps that need them say so)" if failed else ""))
    print(f"Chromium for flythroughs and web-map checks: {browser}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
