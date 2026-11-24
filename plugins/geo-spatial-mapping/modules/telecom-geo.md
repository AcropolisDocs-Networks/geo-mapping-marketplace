---
name: telecom-geo
description: >
  KNOWLEDGE MODULE - read by map-create, webmap-create, geo-analyze and terrain-analysis. Telecom mapping conventions: site-list columns,
  sector wedges, band and technology order, FCC and Census geographies, coverage versus availability wording, status vocabulary,
  confidential site handling, what the layout analytics (spacing, best-server cells, overlap, backhaul, neighbours, drive-test bins, sector
  KPIs, population) measure and do not.
---

# telecom-geo

Read this before mapping towers, sectors, markets or program locations.

## What a site list holds

| Column (any of these names) | Meaning |
|---|---|
| site_id, site, enodeb, gnodeb, cell_site | The site. Several rows can share it (one per sector). |
| lat, lon (latitude, longitude, x, y) | WGS84 decimal degrees. Read as given; nothing is geocoded. Swapped or sign-flipped US values are caught (MAP-TEL-014). |
| azimuth (az, bearing) | Degrees clockwise from true north, 0 to 360. Missing means omni-directional. |
| beamwidth (bw, hbw) | Horizontal beamwidth in degrees; 65 and 120 are common. |
| band, tech, site_type | Color-by fields. Bands order low to high (600, 700, 850, AWS, PCS, 2.5 GHz, C-band, mmWave); technologies 2G, 3G, 4G LTE, 5G NR; site types macro, small cell, DAS, rooftop, COW. |

The duplicate rule: a site id repeated with different azimuths is one site with several sectors, not an error. The same id and azimuth twice is a duplicate (MAP-DATA-005).

## Wedges are drawing, not coverage

A wedge is a pie slice drawn from azimuth, beamwidth and a radius chosen for legibility. It shows direction and sector layout. It is **not** a propagation prediction and says nothing about signal level or service area (MAP-TEL-020, MAP-TEL-021). Say so on the map. Never label wedges "coverage". Coverage needs a model or measured data supplied by the person.

## Coverage versus availability

- **Coverage** is what a network engineering model or drive test says a signal reaches.
- **Availability** is what a provider filed as served, such as the FCC National Broadband Map (NBM) fixed and mobile availability data.
Call FCC NBM data "reported availability", never "coverage" (MAP-TEL-003). It is provider-reported, the hexagon or location unit is a modeled reporting unit, and vintages change twice a year.

## Geographies

- Census cartographic boundary files (state, county, tract) are bundled references: `ref.boundary.us.state.cb20m`, `ref.boundary.us.county.cb`. Join on GEOID: 2 digits state, 5 digits county. Excel often drops the leading zero (04013 becomes 4013); the join repairs it and says so.
- FCC license areas (CMA, MTA, BTA, EA) are not Census units. Use a supplied boundary file; the plugin does not guess them.
- ASR and ULS records give tower and license locations; they are public, positions are as filed, and heights are in meters AGL for towers.

## Status vocabulary (program maps)

Tracker words map to five statuses, each with a color and its own shape: Complete (circle), On track (square), At risk (triangle), Delayed or blocked (diamond), Not started (cross). A word that matches none is drawn as No data and flagged (MAP-TEL-041); the person decides what it means.

## Confidential sites

A client's site list is confidential by default. An external map shows reduced positions only (see site-confidentiality); the true coordinates stay out of the map, the methods note and the data package (MAP-TEL-002, MAP-LIC-007).

## Layout analytics are geometry, not radio

The `network-analysis` pipeline (and `geo-analyze` one op at a time) measures the site layout and the drawn footprints. Each measure
stands in for a radio quantity only loosely, and the map and the notes say so:

| Analysis | What it measures | What it is not |
|---|---|---|
| Site spacing | straight-line distance to the nearest other site; sites per H3 hexagon | inter-site distance along a path, or cell radius |
| Best-server cells | the ground nearer one site than any other (Voronoi), cut to a boundary or the sites' hull | a dominant-server prediction: azimuth, height, band, terrain and power are absent; a site outside the boundary keeps no cell |
| Footprint overlap | where drawn wedges coincide (count, area, the share of area under two or more), within a band and across bands | overshoot, interference or handover zones |
| Backhaul topology | straight lines between linked sites, their geodesic length, links per site (hub degree) | the built fibre or microwave route, line of sight or capacity |
| Neighbour relations | lines between the sites of neighbour-cell pairs with handover successes over attempts | where handovers happen or why they fail |
| Drive-test bins | the statistic (mean, median, p10, p90, count) of the samples that fell in each H3 hexagon; a bin needs `min_count` samples | a prediction for the hexagon; an empty hexagon was not measured, not uncovered |
| Sector KPI | a KPI joined to each sector's wedge, a delta on a diverging ramp centred on zero | a value for every point inside the wedge |
| Population in footprints | households or population apportioned by area into each wedge or each circle of N km (ACS vintage stated) | a served population |

Thresholds people expect beside a drive-test or KPI map, for the reply and the legend title, never hard-coded into the colours (the ramp
is the brand's and the classes come from the data):

| KPI | Good | Fair | Poor |
|---|---|---|---|
| LTE RSRP (dBm) | above -90 | -90 to -105 | below -105 |
| NR SS-RSRP (dBm) | above -95 | -95 to -110 | below -110 |
| LTE RSRQ (dB) | above -10 | -10 to -15 | below -15 |
| SINR (dB) | above 13 | 0 to 13 | below 0 |
| Handover success rate | above 98% | 95 to 98% | below 95% |
| PRB utilisation (busy hour) | below 50% | 50 to 70% | above 70% |

These are common engineering bands, not a standard: the person's own thresholds win, and a map coloured by the brand ramp states the class
breaks in its legend rather than a verdict.

## Scale

The bundled basemap is too generalized below about 1:1,000,000, so city-scale telecom maps use no basemap (INFO MAP-TEL-050). Do not add tiles to make a small map look finished.
