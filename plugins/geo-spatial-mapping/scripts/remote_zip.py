#!/usr/bin/env python3
"""remote_zip: read chosen files out of a large zip on a web server by byte ranges, without downloading the archive (1.17.0).

INEGI publishes Mexico's 2023 geostatistical framework as one 3.2 GB zip: 32 state archives and a national one, `mg_2023_integrado.zip`
(277 MB, stored uncompressed inside), which holds the national states (`00ent`, 12 MB) and municipios (`00mun`, 46 MB). A library entry
names the path to the files and this module fetches only those:

    delivery:
      mode: fetch
      url: https://.../794551067314_s.zip
      remote_members:
        path: [mg_2023_integrado.zip]          # zips inside the archive, outermost first; each must be stored (not compressed)
        prefix: conjunto_de_datos/00ent.       # every member whose name starts with this (the shapefile's .shp, .dbf, .shx, .prj, .cpg)
        crc32: {00ent.shp: 1a2b3c4d, ...}      # optional: each member's CRC-32 as the archive records it, checked as well as the read

It reads the archive's end record and central directory (zip64 included) with Range requests, finds each nested zip's data inside its
parent, reads each wanted member's local header and bytes, inflates them (stored or deflate) and checks the CRC-32 the archive records.
Every request goes through the plugin's guarded client (ogc_http.SafeHTTP: public addresses, byte and time limits), or the caller's.
The bytes fetched in all are capped (`max_bytes`, default 400 MB); a server that ignores Range (answers 200 with the whole file) is refused.
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

MAX_BYTES = 400 * 1024 * 1024
sys.path.insert(0, str(Path(__file__).resolve().parent))
TAIL = 70_000                                  # the end record plus the longest zip comment (65,535) and the zip64 locator


class RemoteZipError(Exception):
    pass


class _Reader:
    """Range reads of one URL, counting the bytes fetched."""

    def __init__(self, url: str, http=None, max_bytes: int = MAX_BYTES):
        if not url.startswith("https://"):
            raise RemoteZipError("only https addresses are read")
        if http is None:                                            # the plugin's guarded client (FCC refused plain urllib's requests)
            import ogc_http as oh
            http = oh.SafeHTTP(limits={"timeout_s": 180, "max_bytes": min(max_bytes, oh.CEILING["max_bytes"])})
        self.url, self.http, self.cap, self.used = url, http, max_bytes, 0
        self.size = None

    def get(self, a: int, b: int) -> bytes:
        n = b - a + 1
        if self.used + n > self.cap:
            raise RemoteZipError(f"the files would take more than {self.cap // (1024 * 1024)} MB; stopped")
        r = self.http.get(self.url, headers={"Range": f"bytes={a}-{b}"}, expect="any", max_bytes=n + 1024)
        status, body = r.status, r.content
        cr = (r.headers or {}).get("content-range") or (r.headers or {}).get("Content-Range")
        if status != 206:
            raise RemoteZipError(f"the server answered HTTP {status} to a byte-range request; it cannot be read in parts")
        if cr and "/" in cr and self.size is None:
            tot = cr.rsplit("/", 1)[1].strip()
            self.size = int(tot) if tot.isdigit() else None
        if len(body) != n:
            raise RemoteZipError(f"asked for {n} bytes at {a}, got {len(body)}")
        self.used += n
        return body

    def total(self) -> int:
        if self.size is None:
            self.get(0, 0)
        if self.size is None:
            raise RemoteZipError("the server did not say the file's size (no Content-Range total)")
        return self.size


def _entries(cd: bytes) -> list[dict]:
    out, p = [], 0
    while cd[p:p + 4] == b"PK\x01\x02":
        method = struct.unpack("<H", cd[p + 10:p + 12])[0]
        crc, csz, usz = struct.unpack("<III", cd[p + 16:p + 28])
        n, e, c = struct.unpack("<HHH", cd[p + 28:p + 34])
        off = struct.unpack("<I", cd[p + 42:p + 46])[0]
        name = cd[p + 46:p + 46 + n].decode("utf-8" if struct.unpack("<H", cd[p + 8:p + 10])[0] & 0x800 else "cp437")
        ex = cd[p + 46 + n:p + 46 + n + e]
        q = 0
        while q + 4 <= len(ex):                                     # zip64 extra: the 0xFFFFFFFF fields, in this order
            hid, hl = struct.unpack("<HH", ex[q:q + 4])
            if hid == 1:
                vals, vi = struct.unpack("<" + "Q" * (hl // 8), ex[q + 4:q + 4 + (hl // 8) * 8]), 0
                if usz == 0xFFFFFFFF:
                    usz, vi = vals[vi], vi + 1
                if csz == 0xFFFFFFFF:
                    csz, vi = vals[vi], vi + 1
                if off == 0xFFFFFFFF:
                    off = vals[vi]
            q += 4 + hl
        out.append({"name": name, "method": method, "crc": crc, "csize": csz, "size": usz, "offset": off})
        p += 46 + n + e + c
    return out


def directory(rd: _Reader, base: int, length: int) -> list[dict]:
    """The members of the zip that occupies [base, base + length) of the remote file."""
    t0 = max(0, length - TAIL)
    tail = rd.get(base + t0, base + length - 1)
    i = tail.rfind(b"PK\x05\x06")
    if i < 0:
        raise RemoteZipError("no zip end record: not a zip, or a damaged one")
    cd_size, cd_off = struct.unpack("<II", tail[i + 12:i + 20])
    z = tail.rfind(b"PK\x06\x07", 0, i)
    if (cd_off == 0xFFFFFFFF or cd_size == 0xFFFFFFFF) and z >= 0:
        rec = struct.unpack("<Q", tail[z + 8:z + 16])[0]
        r64 = rd.get(base + rec, base + rec + 55)
        cd_size, cd_off = struct.unpack("<QQ", r64[40:56])
    return _entries(rd.get(base + cd_off, base + cd_off + cd_size - 1))


def _data_start(rd: _Reader, base: int, m: dict) -> int:
    lh = rd.get(base + m["offset"], base + m["offset"] + 29)
    if lh[:4] != b"PK\x03\x04":
        raise RemoteZipError(f"{m['name']}: no local header where the directory points")
    n, e = struct.unpack("<HH", lh[26:30])
    return base + m["offset"] + 30 + n + e


def extract(url: str, path: list[str], prefix: str, dest: Path, *, http=None, crc32: dict | None = None, max_bytes: int = MAX_BYTES) -> dict:
    """Fetch the members under `prefix` of the zip reached through `path` (nested stored zips) into dest. -> {files, fetched_bytes, members}"""
    rd = _Reader(url, http, max_bytes)
    base, length = 0, rd.total()
    for step in path or []:
        m = next((x for x in directory(rd, base, length) if x["name"] == step), None)
        if m is None:
            raise RemoteZipError(f"'{step}' is not in the archive")
        if m["method"] != 0:
            raise RemoteZipError(f"'{step}' is compressed inside its parent, so its parts cannot be reached by range")
        base, length = _data_start(rd, base, m), m["csize"]
    wanted = [m for m in directory(rd, base, length) if m["name"].startswith(prefix) and not m["name"].endswith("/")]
    if not wanted:
        raise RemoteZipError(f"no member starts with '{prefix}'")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    files = []
    for m in wanted:
        start = _data_start(rd, base, m)
        raw = rd.get(start, start + m["csize"] - 1) if m["csize"] else b""
        if m["method"] == 0:
            data = raw
        elif m["method"] == 8:
            data = zlib.decompressobj(-15).decompress(raw, m["size"] + 1)
        else:
            raise RemoteZipError(f"{m['name']}: compression method {m['method']} is not read")
        if len(data) != m["size"] or (zlib.crc32(data) & 0xFFFFFFFF) != m["crc"]:
            raise RemoteZipError(f"{m['name']}: the bytes read do not match the archive's size and CRC-32")
        leaf = m["name"].rsplit("/", 1)[-1]
        want = (crc32 or {}).get(leaf)
        if want is not None and int(str(want), 16) != m["crc"]:
            raise RemoteZipError(f"{leaf}: CRC-32 {m['crc']:08x} is not the {want} the entry recorded; the publisher may have replaced the file")
        (dest / leaf).write_bytes(data)
        files.append(str(dest / leaf))
    return {"files": files, "fetched_bytes": rd.used, "members": {m["name"].rsplit("/", 1)[-1]: f"{m['crc']:08x}" for m in wanted}}
