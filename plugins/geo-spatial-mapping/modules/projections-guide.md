---
name: projections-guide
description: >
  KNOWLEDGE MODULE - read by map-create, geo-inspect, geo-convert and geo-analyze. Which coordinate system for which job, the distortion
  each trades, the common EPSG codes (4326, 3857, 5070, 4269, 9311, UTM, State Plane), datum caveats (NAD27, NAD83, WGS 84) and how this
  plugin picks one.
---

# projections-guide

Read this when a person asks which projection to use, when `crs_engine.choose_display` needs explaining, or when coordinates land in the
wrong place. The picker is `scripts/crs_engine.py`; its rationale text goes into the map's report and the MapSpec's `crs.rationale`.

## Three coordinate systems on every map

| Role | MapSpec key | What it is for |
|---|---|---|
| Data CRS | read from each file | What the coordinates are in. A file with none is assumed WGS 84 only for lon/lat CSV, with a finding |
| Display CRS | `crs.display` (default `auto`) | How the map is drawn. Chosen for the extent and purpose |
| Measure CRS | `crs.measure` | Where lengths and areas are computed. Area and length are geodesic by default (`geo_ops`), so this is rarely needed |

## How auto picks (purpose thematic, reference or engineering)

| Extent | Pick | Why |
|---|---|---|
| A tile basemap is shown | EPSG:3857 Web Mercator | Tiles exist only in it; areas distort away from the equator |
| The world | EPSG:8857 Equal Earth (thematic) or Robinson (reference) | Equal area for values per area; Robinson for locations |
| Lower 48, regional or national | EPSG:5070 NAD83 / Conus Albers | Equal area, federal practice |
| United States with Alaska or Hawaii | EPSG:9311 US National Atlas Equal Area | One equal-area projection for all three (the code that replaced EPSG:2163) |
| Alaska; Hawaii | EPSG:3338 Alaska Albers; ESRI:102007 Hawaii Albers | The standard equal-area projections there |
| Local (under 6 degrees wide) | UTM zone of the centre (NAD83 zones in North America) | Conformal, one rule everywhere; area error under about 0.1 percent in a zone |
| Local US, engineering (`crs.prefer: state_plane` or purpose engineering) | NAD83 State Plane zone holding the whole extent | The survey grid engineers and counties work in; refused back to UTM when the extent leaves one zone |
| Polar | EPSG:3413 or 3031 polar stereographic | No antimeridian cut, low distortion near the pole |
| Regional elsewhere | Lambert azimuthal equal-area centred on the extent | Equal area, low distortion near the centre |

A person's own choice (`crs.display: EPSG:xxxx`) is always kept; the rationale says it was asked for.

## Common codes

| Code | Name | Use |
|---|---|---|
| EPSG:4326 | WGS 84 geographic | Storage and exchange (GeoJSON is always this). Never for display: degrees are not distances |
| EPSG:4269 | NAD83 geographic | US federal data (TIGER/Line). Differs from WGS 84 by about a metre: irrelevant on a map, relevant to survey work |
| EPSG:3857 | Web Mercator | Tiles only. Greenland looks like Africa; never compute area in it |
| EPSG:5070 | NAD83 / Conus Albers | Thematic maps of the lower 48 |
| EPSG:9311 | US National Atlas Equal Area | National maps with Alaska and Hawaii |
| EPSG:26912 | NAD83 / UTM zone 12N | Arizona, Utah west of 108°W (EPSG:269xx for zone xx in North America; 326xx WGS 84 north, 327xx south) |
| EPSG:2223 | NAD83 / Arizona Central (ft) | State Plane: Arizona legislated international feet; most states use US survey feet (see below) |

## State Plane

Zones follow state and county lines; a state has one to ten. Units: metres in the EPSG definitions of NAD83 zones, with separate codes in US
survey feet or international feet where states legislated them (the US survey foot was retired by NIST and NGS at the end of 2022, but existing
data stays in it). `crs_engine.choose_state_plane` reads the zones from PROJ's EPSG database (areas of use), so the list cannot drift; where two
zones overlap along a shared edge the smaller is taken. NGS's State Plane Coordinate System of 2022 (SPCS2022, on NATRF2022) is not in use yet:
the new datums are delayed, and maps here stay on NAD83.

## Datum caveats

- **NAD27 to NAD83:** shifts of tens of metres in the lower 48 (more in Alaska); grid-based (NADCON). A legacy file drawn without its datum
  lands in the wrong field. Never assume NAD27 is NAD83.
- **NAD83 to WGS 84:** about one to two metres, changing over time (plate motion). For maps, equal; for survey or engineering, use the
  realization the data names (NAD83(2011), WGS 84 (G2139)).
- **Axis order:** EPSG:4326 is latitude first by the standard; GeoJSON, KML and most software use longitude first. WFS 2.0 and WMS 1.3.0
  follow the standard; the service code handles it (`ogc_client`), and CRS84 is unambiguous.
- **Antimeridian:** an extent across 180° (Aleutians, Fiji, Pacific) needs a projection centred on it and data split at the line
  (`geo_ops antimeridian`).

## Telling the person

Name the pick and its reason in one sentence ("Conus Albers, because the map compares rates across states and it keeps areas true"). Mention
the alternative only when it matters for their use (State Plane for engineering inside one state; Web Mercator only with tiles).
