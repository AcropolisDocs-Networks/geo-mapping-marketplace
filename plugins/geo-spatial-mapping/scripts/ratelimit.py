#!/usr/bin/env python3
"""ratelimit: one request budget per host, shared by every process of the person's (1.3.0, R1-REQ).

    ratelimit.py status [HOST]          the calls counted in the last minute and when the next one may go

A library entry or a built-in system declares its limits (`delivery.api.limits` or `delivery.requires_credential.limits_rule`):

    limits: {calls_per_minute: 10, sequential: true}

`calls_per_minute`: a sliding window of 60 seconds. A call waits for a free slot rather than failing (the caller says it waited: MAP-FCC-004 for
the FCC). `sequential`: calls to that host never overlap, even from two processes: the slot is held for the call's whole length.
The state is `.geo-mapping/ratelimits/<host>.json` (the times of the calls in the last minute), read and written under an exclusive lock on
`<host>.lock` (fcntl on Linux and macOS, msvcrt on Windows), so parallel processes share one budget.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

WINDOW_S = 60.0
NOW = time.time            # the wall clock the budget is kept in (shared across processes); tests replace it


def state_dir() -> Path:
    d = gc.workdir() / "ratelimits"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe(host: str) -> str:
    return re.sub(r"[^a-z0-9.-]", "_", str(host).lower()) or "host"


@contextlib.contextmanager
def _locked(path: Path):
    """An exclusive lock on `path` across processes (blocks until it is free)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    try:
        if os.name == "nt":                                      # pragma: no cover - Windows
            import msvcrt
            fh.seek(0)
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield fh
    finally:
        try:
            if os.name == "nt":                                  # pragma: no cover
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _read(p: Path) -> list[float]:
    try:
        return [float(x) for x in json.loads(p.read_text(encoding="utf-8")).get("calls", [])]
    except (OSError, ValueError, AttributeError):
        return []


def _write(p: Path, calls: list[float]) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"calls": calls, "window_s": WINDOW_S}), encoding="utf-8")
    os.replace(tmp, p)


def acquire(host: str, limits: dict | None, *, clock=None, sleep=time.sleep) -> float:
    """Take one call from the host's budget, waiting for a free slot when the last minute is full. Returns the seconds waited (0.0: none)."""
    per_min = int((limits or {}).get("calls_per_minute") or 0)
    if per_min <= 0:
        return 0.0
    clock = clock or NOW
    base = state_dir() / _safe(host)
    waited = 0.0
    while True:
        with _locked(base.with_suffix(".lock")):
            now = clock()
            calls = [t for t in _read(base.with_suffix(".json")) if now - t < WINDOW_S]
            if len(calls) < per_min:
                calls.append(now)
                _write(base.with_suffix(".json"), calls)
                return waited
            wait = max(0.0, calls[0] + WINDOW_S - now) + 0.01
        sleep(wait)                                              # outside the lock, so another process can read the state meanwhile
        waited += wait


@contextlib.contextmanager
def slot(host: str, limits: dict | None, *, clock=None, sleep=time.sleep):
    """One call's slot: the budget taken first, and with `sequential` the host held for the call's whole length. Yields the seconds waited."""
    waited = acquire(host, limits, clock=clock, sleep=sleep)
    if (limits or {}).get("sequential"):
        with _locked(state_dir() / (_safe(host) + ".seq.lock")):
            yield waited
    else:
        yield waited


def status(host: str, *, clock=None) -> dict:
    base = state_dir() / _safe(host)
    now = (clock or NOW)()
    calls = [t for t in _read(base.with_suffix(".json")) if now - t < WINDOW_S]
    return {"host": host, "calls_last_minute": len(calls), "oldest_s_ago": round(now - calls[0], 1) if calls else None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("op", choices=["status"])
    ap.add_argument("host", nargs="?")
    a = ap.parse_args(argv)
    hosts = [a.host] if a.host else sorted(p.stem for p in state_dir().glob("*.json"))
    print(json.dumps([status(h) for h in hosts], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
