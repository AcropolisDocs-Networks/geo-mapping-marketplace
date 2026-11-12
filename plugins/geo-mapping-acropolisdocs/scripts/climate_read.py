#!/usr/bin/env python3
"""climate_read: a climate file (NetCDF or GRIB, from the Copernicus CDS or supplied) as one GeoTIFF per variable (1.3.0, R1-CDS).

    climate_read.py FILE --out DIR [--variable t2m] [--transform kelvin_to_celsius|m_to_mm|per_second_to_per_day]

NetCDF is read through rasterio (GDAL's netCDF driver): one subdataset per variable, the time dimension as bands. The CF attributes are applied
(scale_factor, add_offset, _FillValue, units). Longitudes from 0 to 360 are wrapped to -180 to 180. Each variable becomes a GeoTIFF (bands =
times) with a sidecar JSON of the band dates, the units and what was done. GRIB is read through GDAL's GRIB driver. Without those drivers the read
stops (MAP-CDS-008) and says how to get them (doctor).

Unit helpers (`values.transform`): kelvin_to_celsius (K - 273.15), m_to_mm (x 1000), per_second_to_per_day (x 86,400). A transformed value is
"Contains modified Copernicus Climate Change Service information" in the credit.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gmap_common as gc  # noqa: E402

TRANSFORMS = {"kelvin_to_celsius": (1.0, -273.15, "degC"), "m_to_mm": (1000.0, 0.0, "mm"), "per_second_to_per_day": (86400.0, 0.0, "per day")}
_UNITS_SINCE = re.compile(r"^\s*(seconds|minutes|hours|days)\s+since\s+(\d{4}-\d{1,2}-\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?", re.I)


class ReadError(Exception):
    def __init__(self, message: str, rule: str = "MAP-CDS-008"):
        super().__init__(message)
        self.rule = rule


def drivers() -> dict:
    import rasterio
    with rasterio.Env() as env:
        d = env.drivers()
    return {"netCDF": "netCDF" in d, "GRIB": "GRIB" in d}


def _times(tags: dict, n: int) -> list[str | None]:
    """Band dates from GDAL's netCDF tags: NETCDF_DIM_time_VALUES and time#units ('hours since 1900-01-01 00:00:00')."""
    vals = tags.get("NETCDF_DIM_time_VALUES") or tags.get("NETCDF_DIM_valid_time_VALUES")
    units = tags.get("time#units") or tags.get("valid_time#units") or ""
    m = _UNITS_SINCE.match(units)
    if not vals or not m:
        return [None] * n
    nums = [float(x) for x in re.findall(r"-?[\d.]+(?:e[-+]?\d+)?", vals)]
    base = dt.datetime.fromisoformat(f"{m.group(2)}T{int(m.group(3) or 0):02d}:{m.group(4) or '00'}:{m.group(5) or '00'}")
    unit = m.group(1).lower()
    out = []
    for v in nums[:n]:
        t = base + dt.timedelta(**{unit: v})
        out.append(t.date().isoformat() if t.time() == dt.time(0) else t.isoformat())
    return out + [None] * (n - len(out))


def read(path, out_dir, *, variable: str | None = None, transform: str | None = None) -> dict:
    """-> {files: [{variable, tif, sidecar, bands, dates, units}], notes}."""
    import numpy as np
    import rasterio
    from rasterio.transform import Affine
    path, out_dir = Path(path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    head = path.read_bytes()[:8]
    kind = "grib" if head[:4] == b"GRIB" else "netcdf"
    dv = drivers()
    if kind == "netcdf" and not dv["netCDF"] or kind == "grib" and not dv["GRIB"]:
        raise ReadError(f"this computer's GDAL has no {'netCDF' if kind == 'netcdf' else 'GRIB'} driver, so the file cannot be read here; run doctor --fix "
                        "(rasterio's wheels carry both)")
    if transform and transform not in TRANSFORMS:
        raise ReadError(f"transform '{transform}' is not one of {', '.join(TRANSFORMS)}", "MAP-CDS-002")
    with rasterio.open(path) as ds:
        subs = list(ds.subdatasets) if kind == "netcdf" else []
    targets = []
    if subs:
        for sd_ in subs:
            name = sd_.rsplit(":", 1)[-1]
            if name in ("longitude", "latitude", "lon", "lat", "time", "valid_time", "expver", "number"):
                continue
            if variable and name != variable:
                continue
            targets.append((name, sd_))
    else:                                                      # one variable: GDAL opens it directly; its name is in the tags (t2m#units)
        with rasterio.open(path) as ds:
            names = sorted({k.split("#", 1)[0] for k in ds.tags() if "#" in k} - {"longitude", "latitude", "lon", "lat", "time", "valid_time", "NC_GLOBAL"})
        if variable and names and variable not in names:
            raise ReadError(f"variable '{variable}' is not in {path.name} ({', '.join(names)})", "MAP-CDS-002")
        targets.append((variable or (names[0] if names else path.stem), str(path)))
    if not targets:
        raise ReadError(f"variable '{variable}' is not in {path.name}", "MAP-CDS-002")
    files, notes = [], []
    for name, src in targets:
        with rasterio.open(src) as ds:
            tags = ds.tags()
            arr = ds.read(masked=True).astype("float64")
            scales, offsets = ds.scales, ds.offsets
            units = tags.get(f"{name}#units") or (ds.tags(1).get("GRIB_UNIT") or "").strip("[]") or None
            for b in range(arr.shape[0]):                      # CF scale_factor and add_offset (GDAL hands them over as scale and offset)
                arr[b] = arr[b] * float(scales[b] or 1.0) + float(offsets[b] or 0.0)
            tr = ds.transform
            crs = ds.crs or "EPSG:4326"
            dates = _times(tags, arr.shape[0]) if kind == "netcdf" else [ds.tags(b + 1).get("GRIB_VALID_TIME") for b in range(arr.shape[0])]
        w = arr.shape[2]
        x0 = tr.c
        x1 = tr.c + tr.a * w
        if x1 > 180.0 + abs(tr.a):                             # 0 to 360: roll the columns east of 180 to the west
            split = int(round((180.0 - x0) / tr.a))
            arr = np.ma.concatenate([arr[:, :, split:], arr[:, :, :split]], axis=2)
            tr = Affine(tr.a, tr.b, x0 + split * tr.a - 360.0, tr.d, tr.e, tr.f)
            notes.append(f"{name}: longitudes 0 to 360 wrapped to -180 to 180")
        if tr.e > 0:                                           # south-up: flip to north-up
            arr = arr[:, ::-1, :]
            tr = Affine(tr.a, tr.b, tr.c, tr.d, -tr.e, tr.f + tr.e * arr.shape[1])
        if transform:
            k, add, u = TRANSFORMS[transform]
            arr = arr * k + add
            units = u
            notes.append(f"{name}: values {transform.replace('_', ' ')}")
        nod = -9999.0
        tif = out_dir / f"{path.stem}_{name}.tif"
        with rasterio.open(tif, "w", driver="GTiff", width=arr.shape[2], height=arr.shape[1], count=arr.shape[0], dtype="float32", crs=crs, transform=tr,
                           nodata=nod, compress="deflate") as dst:
            dst.write(arr.filled(nod).astype("float32"))
            for b, d in enumerate(dates):
                if d:
                    dst.update_tags(b + 1, date=str(d))
        side = tif.with_suffix(".json")
        side.write_text(json.dumps({"variable": name, "source": path.name, "bands": int(arr.shape[0]), "dates": dates, "units": units,
                                    "transform": transform, "nodata": nod, "notes": notes}, indent=2))
        files.append({"variable": name, "tif": str(tif), "sidecar": str(side), "bands": int(arr.shape[0]), "dates": dates, "units": units})
    return {"files": files, "notes": notes, "kind": kind}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("file")
    ap.add_argument("--out", required=True)
    ap.add_argument("--variable")
    ap.add_argument("--transform", choices=list(TRANSFORMS))
    a = ap.parse_args(argv)
    try:
        res = read(a.file, a.out, variable=a.variable, transform=a.transform)
    except ReadError as ex:
        print(json.dumps({"ok": False, "findings": [{"id": ex.rule, "severity": "BLOCK", "message": str(ex)}]}, indent=2))
        return gc.EXIT_LINT
    print(json.dumps({"ok": True, **res}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
