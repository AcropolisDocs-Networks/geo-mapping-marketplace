#!/usr/bin/env python3
"""pmtiles_extract: an OpenStreetMap basemap file for one window, cut from the Protomaps daily build with HTTP range requests (3.7.0, spec
B17; the Day 23 streets map).

    pmtiles_extract.py plan    --bbox W,S,E,N [--max-zoom 14] [--build latest|YYYYMMDD] [--source URL]
    pmtiles_extract.py extract --bbox W,S,E,N --out FILE.pmtiles [--max-zoom 14] [--build ...] [--cap-mb 200]

The plan reads only the archive's header and the directories that cover the window, so it states the exact size before a tile is fetched. The
extract fetches the tiles (neighbouring ones in one range request), writes a PMTiles v3 file of them (clustered, deduplicated), and records
the build date and the credit: © OpenStreetMap contributors (ODbL), Protomaps. `basemap: {provider: pmtiles, source: pmtiles:FILE}` then draws
it as the print map's ground and in the 3D page, offline. GET with a Range header only, on public addresses, through SafeHTTP.

The PMTiles v3 format (header, Hilbert tile ids, varint directories) is written out here so the plugin needs no extra package; it follows the
specification at github.com/protomaps/PMTiles (spec/v3).
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gmap_common as gc  # noqa: E402

BUILD_HOST = "build.protomaps.com"
BUILD_URL = "https://build.protomaps.com/{date}.pmtiles"
HEADER_LEN = 127
FIRST_READ = 16384                 # the header and the root directory sit in the first 16 KiB of an archive
MERGE_GAP = 65536                  # neighbouring tiles closer than this are fetched in one range request
MAX_RANGE = 8_000_000
CAP_MB = 200.0
MAX_ZOOM = 15
CREDIT = "© OpenStreetMap contributors (ODbL), Protomaps"


class ExtractError(Exception):
    def __init__(self, message: str, rule: str = "MAP-TILE-006"):
        super().__init__(message)
        self.rule = rule


@dataclass
class Entry:
    tile_id: int
    offset: int
    length: int
    run_length: int


# ---------------------------------------------------------------------------------------------------------------- the format
def _rotate(n, x, y, rx, ry):
    if ry == 0:
        if rx != 0:
            x, y = n - 1 - x, n - 1 - y
        x, y = y, x
    return x, y


def zxy_to_tileid(z: int, x: int, y: int) -> int:
    acc = ((1 << (z * 2)) - 1) // 3
    a = z - 1
    while a >= 0:
        s = 1 << a
        rx, ry = s & x, s & y
        acc += ((3 * rx) ^ ry) << a
        x, y = _rotate(s, x, y, rx, ry)
        a -= 1
    return acc


def _read_varint(b: io.BytesIO) -> int:
    shift, out = 0, 0
    while True:
        c = b.read(1)
        if not c:
            raise ExtractError("the archive's directory ends early")
        v = c[0]
        out |= (v & 0x7F) << shift
        if not v & 0x80:
            return out
        shift += 7


def _write_varint(b: io.BytesIO, v: int) -> None:
    while v >= 0x80:
        b.write(bytes([(v & 0x7F) | 0x80]))
        v >>= 7
    b.write(bytes([v]))


def _decompress(buf: bytes, kind: int) -> bytes:
    if kind == 2:
        return gzip.decompress(buf)
    if kind in (0, 1):
        return buf
    raise ExtractError(f"the archive's directories use compression {kind}, which is not read here")


def deserialize_directory(buf: bytes, compression: int = 2) -> list[Entry]:
    b = io.BytesIO(_decompress(buf, compression))
    n = _read_varint(b)
    es, last = [], 0
    for _ in range(n):
        last += _read_varint(b)
        es.append(Entry(last, 0, 0, 0))
    for e in es:
        e.run_length = _read_varint(b)
    for e in es:
        e.length = _read_varint(b)
    for i, e in enumerate(es):
        v = _read_varint(b)
        e.offset = es[i - 1].offset + es[i - 1].length if (i > 0 and v == 0) else v - 1
    return es


def serialize_directory(es: list[Entry]) -> bytes:
    b = io.BytesIO()
    _write_varint(b, len(es))
    last = 0
    for e in es:
        _write_varint(b, e.tile_id - last)
        last = e.tile_id
    for e in es:
        _write_varint(b, e.run_length)
    for e in es:
        _write_varint(b, e.length)
    for i, e in enumerate(es):
        _write_varint(b, 0 if (i > 0 and e.offset == es[i - 1].offset + es[i - 1].length) else e.offset + 1)
    return gzip.compress(b.getvalue(), mtime=0)


def _u64(b, p):
    return int.from_bytes(b[p:p + 8], "little")


def _i32(b, p):
    return int.from_bytes(b[p:p + 4], "little", signed=True)


def deserialize_header(b: bytes) -> dict:
    if b[:7] != b"PMTiles" or len(b) < HEADER_LEN:
        raise ExtractError("the address is not a PMTiles archive")
    if b[7] != 3:
        raise ExtractError(f"the archive is PMTiles version {b[7]}; version 3 is read here")
    return {"root_offset": _u64(b, 8), "root_length": _u64(b, 16), "metadata_offset": _u64(b, 24), "metadata_length": _u64(b, 32),
            "leaf_offset": _u64(b, 40), "leaf_length": _u64(b, 48), "tile_data_offset": _u64(b, 56), "tile_data_length": _u64(b, 64),
            "addressed": _u64(b, 72), "entries": _u64(b, 80), "contents": _u64(b, 88), "clustered": b[96] == 1, "internal_compression": b[97],
            "tile_compression": b[98], "tile_type": b[99], "min_zoom": b[100], "max_zoom": b[101], "min_lon_e7": _i32(b, 102),
            "min_lat_e7": _i32(b, 106), "max_lon_e7": _i32(b, 110), "max_lat_e7": _i32(b, 114), "center_zoom": b[118],
            "center_lon_e7": _i32(b, 119), "center_lat_e7": _i32(b, 123)}


def serialize_header(h: dict) -> bytes:
    out = io.BytesIO()
    out.write(b"PMTiles\x03")
    for k in ("root_offset", "root_length", "metadata_offset", "metadata_length", "leaf_offset", "leaf_length", "tile_data_offset",
              "tile_data_length", "addressed", "entries", "contents"):
        out.write(int(h.get(k, 0)).to_bytes(8, "little"))
    out.write(bytes([1 if h.get("clustered", True) else 0, int(h.get("internal_compression", 2)), int(h.get("tile_compression", 2)),
                     int(h.get("tile_type", 1)), int(h["min_zoom"]), int(h["max_zoom"])]))
    for k in ("min_lon_e7", "min_lat_e7", "max_lon_e7", "max_lat_e7"):
        out.write(int(h[k]).to_bytes(4, "little", signed=True))
    out.write(bytes([int(h.get("center_zoom", h["min_zoom"]))]))
    for k in ("center_lon_e7", "center_lat_e7"):
        out.write(int(h[k]).to_bytes(4, "little", signed=True))
    return out.getvalue()


def find(es: list[Entry], tid: int) -> Entry | None:
    lo, hi = 0, len(es) - 1
    while lo <= hi:
        k = (lo + hi) >> 1
        c = tid - es[k].tile_id
        if c > 0:
            lo = k + 1
        elif c < 0:
            hi = k - 1
        else:
            return es[k]
    if hi >= 0:
        if es[hi].run_length == 0:
            return es[hi]
        if tid - es[hi].tile_id < es[hi].run_length:
            return es[hi]
    return None


# ---------------------------------------------------------------------------------------------------------------- the window
def tile_range(bbox, z: int) -> tuple[int, int, int, int]:
    w, s, e, n = bbox
    def tx(lon):
        return int(math.floor((lon + 180.0) / 360.0 * (1 << z)))

    def ty(lat):
        lat = max(min(lat, 85.0511), -85.0511)
        r = math.radians(lat)
        return int(math.floor((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * (1 << z)))
    hi = (1 << z) - 1
    return max(0, tx(w)), max(0, ty(n)), min(hi, tx(e)), min(hi, ty(s))


def tiles_of(bbox, min_zoom: int, max_zoom: int) -> list[tuple[int, int, int]]:
    out = []
    for z in range(min_zoom, max_zoom + 1):
        x0, y0, x1, y1 = tile_range(bbox, z)
        out += [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    return out


# ---------------------------------------------------------------------------------------------------------------- reading
class Source:
    """Ranges of an archive: a local file, or an https address read through SafeHTTP with a Range header."""

    def __init__(self, where: str, http=None):
        self.where = where
        self.http = http
        self.requests = 0
        self.bytes = 0
        if not where.lower().startswith("https://"):
            p = Path(where).expanduser()
            if not p.is_file():
                raise ExtractError(f"the archive {where} is not a file here or an https address")
            self.path = p
        else:
            self.path = None

    def read(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        self.requests += 1
        if self.path is not None:
            with open(self.path, "rb") as f:
                f.seek(offset)
                b = f.read(length)
        else:
            import ogc_http as oh
            h = self.http or oh.SafeHTTP(limits={"timeout_s": 120, "max_bytes": max(MAX_RANGE * 2, length + 1024), "retries": 2})
            self.http = h
            try:
                r = h.get(self.where, headers={"Range": f"bytes={offset}-{offset + length - 1}"}, expect="any", max_bytes=length + 1024)
            except oh.ServiceError as ex:
                raise ExtractError(f"the build did not answer: {ex}", ex.rule or "MAP-SVC-013") from None
            b = r.content
            if r.status == 200 and len(b) > length:            # a server that ignores Range sends the whole file: never accepted
                raise ExtractError("the server does not answer range requests, so the build cannot be cut here")
        self.bytes += len(b)
        return b


def latest_build(http=None, today: dt.date | None = None) -> str:
    """The address of the newest daily build (a HEAD to each of the last eight days, newest first)."""
    import ogc_http as oh
    h = http or oh.SafeHTTP(limits={"timeout_s": 30, "max_bytes": 1024, "retries": 0})
    d = today or dt.date.today()
    for k in range(8):
        url = BUILD_URL.format(date=(d - dt.timedelta(days=k)).strftime("%Y%m%d"))
        try:
            h.get(url, method="HEAD", expect="any")
            return url
        except oh.ServiceError:
            continue
    raise ExtractError(f"no Protomaps daily build answered at {BUILD_HOST} for the last eight days; name one with build=YYYYMMDD or source=URL")


def _entries_for(src: Source, h: dict, tids: list[int]) -> dict[int, Entry]:
    """{tile id: its entry (offset relative to the tile data section)} for the tiles the archive has."""
    root_raw = src.read(h["root_offset"], h["root_length"])
    root = deserialize_directory(root_raw, h["internal_compression"])
    leaves: dict[int, list[Entry]] = {}
    out: dict[int, Entry] = {}
    for tid in tids:
        es = root
        for _depth in range(4):
            e = find(es, tid)
            if e is None:
                break
            if e.run_length > 0:
                out[tid] = e
                break
            if e.offset not in leaves:
                leaves[e.offset] = deserialize_directory(src.read(h["leaf_offset"] + e.offset, e.length), h["internal_compression"])
            es = leaves[e.offset]
    return out


def plan(bbox, max_zoom: int = 14, *, source: str = "", build: str = "latest", http=None, min_zoom: int = 0) -> dict:
    """The tiles, the exact bytes and the requests the extract will take, from the header and the directories only."""
    if not (0 <= int(max_zoom) <= MAX_ZOOM):
        raise ExtractError(f"max_zoom is 0 to {MAX_ZOOM}")
    w, s, e, n = (float(v) for v in bbox)
    if not (-180 <= w < e <= 180 and -85.06 <= s < n <= 85.06):
        raise ExtractError("bbox is W,S,E,N in degrees, W < E and S < N")
    url = source or (latest_build(http) if build == "latest" else BUILD_URL.format(date=str(build)))
    src = Source(url, http)
    h = deserialize_header(src.read(0, FIRST_READ)[:HEADER_LEN])
    if h["tile_type"] != 1:
        raise ExtractError("the archive does not hold vector tiles (MVT)")
    mz = min(int(max_zoom), h["max_zoom"])
    want = tiles_of((w, s, e, n), max(min_zoom, h["min_zoom"]), mz)
    tids = sorted({zxy_to_tileid(*t) for t in want})
    found = _entries_for(src, h, tids)
    total = sum(found[t].length for t in found)
    uniq = sum(v for v in {(found[t].offset, found[t].length): found[t].length for t in found}.values())
    return {"ok": True, "source": url if url.startswith("https://") else Path(url).name, "build": _build_date(url), "bbox": [w, s, e, n],
            "min_zoom": max(min_zoom, h["min_zoom"]), "max_zoom": mz, "tiles": len(found), "bytes": uniq, "mb": round(uniq / 1048576, 1),
            "requests_so_far": src.requests, "credit": CREDIT, "_src": src, "_h": h, "_found": found, "_tids": tids, "_raw_total": total}


def _build_date(url: str) -> str | None:
    stem = Path(url.split("?")[0]).stem
    return stem if stem.isdigit() and len(stem) == 8 else None


# ---------------------------------------------------------------------------------------------------------------- writing
def write(out: Path, header: dict, tiles: dict[int, bytes], metadata: dict) -> dict:
    """A clustered PMTiles v3 file: identical tiles stored once, runs of the same content run-length encoded."""
    data = io.BytesIO()
    by_hash: dict[str, tuple[int, int]] = {}
    es: list[Entry] = []
    for tid in sorted(tiles):
        b = tiles[tid]
        hsh = hashlib.sha256(b).hexdigest()
        if hsh in by_hash:
            off, ln = by_hash[hsh]
        else:
            off, ln = data.tell(), len(b)
            data.write(b)
            by_hash[hsh] = (off, ln)
        if es and es[-1].offset == off and es[-1].tile_id + es[-1].run_length == tid:
            es[-1].run_length += 1
        else:
            es.append(Entry(tid, off, ln, 1))
    root = serialize_directory(es)
    leaf = b""
    if len(root) > FIRST_READ - HEADER_LEN:
        size = 4096
        while True:
            leaves, rents, pos = [], [], 0
            for i in range(0, len(es), size):
                chunk = serialize_directory(es[i:i + size])
                rents.append(Entry(es[i].tile_id, pos, len(chunk), 0))
                leaves.append(chunk)
                pos += len(chunk)
            root = serialize_directory(rents)
            if len(root) <= FIRST_READ - HEADER_LEN:
                leaf = b"".join(leaves)
                break
            size *= 2
    meta = gzip.compress(json.dumps(metadata).encode(), mtime=0)
    h = dict(header)
    h.update(root_offset=HEADER_LEN, root_length=len(root), metadata_offset=HEADER_LEN + len(root), metadata_length=len(meta),
             leaf_offset=HEADER_LEN + len(root) + len(meta), leaf_length=len(leaf), tile_data_offset=HEADER_LEN + len(root) + len(meta) + len(leaf),
             tile_data_length=data.tell(), addressed=len(tiles), entries=len(es), contents=len(by_hash), clustered=True, internal_compression=2)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        f.write(serialize_header(h))
        f.write(root)
        f.write(meta)
        f.write(leaf)
        f.write(data.getvalue())
    return {"tiles": len(tiles), "entries": len(es), "contents": len(by_hash), "bytes": out.stat().st_size}


def extract(bbox, out: Path, max_zoom: int = 14, *, source: str = "", build: str = "latest", cap_mb: float = CAP_MB, http=None) -> dict:
    p = plan(bbox, max_zoom, source=source, build=build, http=http)
    if p["bytes"] / 1048576 > float(cap_mb):
        raise ExtractError(f"the extract would be {p['mb']} MB, over the {cap_mb:g} MB cap: a smaller window or a lower max_zoom (now {p['max_zoom']})", "MAP-TILE-006")
    src, h, found = p["_src"], p["_h"], p["_found"]
    spans = sorted({(e.offset, e.length) for e in found.values()})
    blobs: dict[tuple[int, int], bytes] = {}
    i = 0
    while i < len(spans):
        a0 = spans[i][0]
        end = spans[i][0] + spans[i][1]
        j = i + 1
        while j < len(spans) and spans[j][0] - end < MERGE_GAP and (spans[j][0] + spans[j][1] - a0) < MAX_RANGE:
            end = max(end, spans[j][0] + spans[j][1])
            j += 1
        buf = src.read(h["tile_data_offset"] + a0, end - a0)
        for off, ln in spans[i:j]:
            blobs[(off, ln)] = buf[off - a0: off - a0 + ln]
        i = j
    tiles = {tid: blobs[(e.offset, e.length)] for tid, e in found.items()}
    try:
        meta = json.loads(_decompress(src.read(h["metadata_offset"], h["metadata_length"]), h["internal_compression"]) or b"{}")
    except Exception:  # noqa: BLE001 - the extract keeps going with the minimum the page needs
        meta = {}
    w, s, e, n = p["bbox"]
    meta = {k: v for k, v in (meta or {}).items() if k in ("vector_layers", "name", "description", "attribution", "type", "version", "planetiler:osm:osmosisreplicationtime")}
    meta.update({"attribution": CREDIT, "gm:extract": {"bbox": p["bbox"], "max_zoom": p["max_zoom"], "build": p["build"], "source": p["source"],
                                                          "made": (dt.date.today().isoformat())}})
    hdr = {**h, "min_zoom": p["min_zoom"], "max_zoom": p["max_zoom"], "min_lon_e7": int(w * 1e7), "min_lat_e7": int(s * 1e7),
           "max_lon_e7": int(e * 1e7), "max_lat_e7": int(n * 1e7), "center_zoom": max(p["min_zoom"], min(p["max_zoom"], 12)),
           "center_lon_e7": int((w + e) / 2 * 1e7), "center_lat_e7": int((s + n) / 2 * 1e7)}
    res = write(Path(out), hdr, tiles, meta)
    rec = {"ok": True, "file": str(out), "build": p["build"], "source": p["source"], "bbox": p["bbox"], "min_zoom": p["min_zoom"], "max_zoom": p["max_zoom"],
           "requests": src.requests, "bytes_read": src.bytes, "credit": CREDIT, "licence": "ODbL (OpenStreetMap)", **res,
           "use_it": "basemap: " + "{" + f"provider: pmtiles, source: pmtiles:{Path(out).name}" + "}"}
    Path(str(out) + ".json").write_text(json.dumps({k: v for k, v in rec.items() if not k.startswith("_")}, indent=2), encoding="utf-8")
    return rec


def public(p: dict) -> dict:
    return {k: v for k, v in p.items() if not k.startswith("_")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("op", choices=["plan", "extract"])
    ap.add_argument("--bbox", required=True)
    ap.add_argument("--max-zoom", type=int, default=14)
    ap.add_argument("--build", default="latest")
    ap.add_argument("--source", default="")
    ap.add_argument("--out")
    ap.add_argument("--cap-mb", type=float, default=CAP_MB)
    a = ap.parse_args(argv)
    try:
        bbox = [float(v) for v in a.bbox.split(",")]
        if a.op == "plan":
            print(json.dumps(public(plan(bbox, a.max_zoom, source=a.source, build=a.build)), indent=2))
        else:
            if not a.out:
                print("extract needs --out FILE.pmtiles", file=sys.stderr)
                return gc.EXIT_USAGE
            print(json.dumps(extract(bbox, Path(a.out), a.max_zoom, source=a.source, build=a.build, cap_mb=a.cap_mb), indent=2))
        return gc.EXIT_OK
    except (ExtractError, ValueError) as ex:
        print(f"pmtiles_extract: {ex}", file=sys.stderr)
        return gc.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
