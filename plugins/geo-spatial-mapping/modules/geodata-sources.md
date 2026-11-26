---
name: geodata-sources
description: >
  KNOWLEDGE MODULE - read by map-create, geo-find-data and reference-curate. What the reference library holds today (generated from the
  entries) and the gotchas of the common public sources: Census, TIGER, Natural Earth, FCC, HIFLD, OSM.
---

# geodata-sources

Read this to answer "what data do we have?" or to explain why a source behaves as it does. The tables are generated from the
library at build time, so they always match the entries; do not edit them by hand. For a ranked answer to a specific need use `geo-find-data`.

Library: 266 entries (257 active); basemap-provider 18, boundary 53, boundary-derived 2, context 37, crosswalk 1, gazetteer 12, infrastructure 21, map-type 13, palette 2, public-service 56, statistic 26, symbol-set 1, template 6, terrain 18.

### Usable now

| Entry | What it is | Publisher, vintage | How it arrives | License | Join key |
|---|---|---|---|---|---|
| `ref.boundary-derived.us.cbrs-pal.fcc` | FCC CBRS Priority Access Licenses (Auction 105) by county | FCC, Auction 105 close (2020-08-25); PAL terms run 10 years | downloaded on first use | public-domain | county_fips (string, 5 chars) |
| `ref.boundary-derived.us.license-area.fcc` | FCC Cellular Market Areas (CMA) and other licence areas | FCC, 2016 service (CMA definitions are static) | web service | public-domain | CMA (integer) |
| `ref.boundary.ca.census-division.statcan2021` | Canadian census divisions (StatCan 2021 cartographic boundary file) | Statistics Canada, 2021 Census | web service | attribution | CDUID (string, 4 chars) |
| `ref.boundary.ca.csd.statcan2021` | Canadian census subdivisions (StatCan 2021 cartographic boundary file) | Statistics Canada, 2021 Census | web service | attribution | CSDUID (string, 7 chars) |
| `ref.boundary.ca.province.statcan2021` | Canadian provinces and territories (StatCan 2021 cartographic boundary file) | Statistics Canada, 2021 Census | web service | attribution | PRUID (string, 2 chars) |
| `ref.boundary.europe.nuts.gisco2024` | European NUTS regions 2024 (Eurostat GISCO) | Eurostat GISCO; administrative boundaries © EuroGeographics, NUTS 2024 | downloaded on first use | restricted | NUTS_ID (string) |
| `ref.boundary.fr.department.ign-adminexpress` | IGN ADMIN EXPRESS COG, small scale: French departments | Institut national de l'information géographique et forestière (IGN), COG 2026 | downloaded on first use | open | code_insee (string) |
| `ref.boundary.fr.region.ign-adminexpress` | IGN ADMIN EXPRESS COG, small scale: French regions | Institut national de l'information géographique et forestière (IGN), COG 2026 | downloaded on first use | open | code_insee (string) |
| `ref.boundary.mx.municipio.inegi2023` | Mexican municipalities (INEGI Marco Geoestadístico 2023) | Instituto Nacional de Estadística y Geografía (INEGI), December 2023 | downloaded on first use | attribution | CVEGEO (string, 5 chars) |
| `ref.boundary.mx.state.inegi2023` | Mexican states (INEGI Marco Geoestadístico 2023) | Instituto Nacional de Estadística y Geografía (INEGI), December 2023 | downloaded on first use | attribution | CVE_ENT (string, 2 chars) |
| `ref.boundary.uk.admin.os-boundary-line` | Boundary-Line: administrative and electoral boundaries of Great Britain (Ordnance Survey) | Ordnance Survey, 2026-10 | you supply the file | attribution | Census_Code (string) |
| `ref.boundary.uk.lad.ons` | ONS local authority districts, December 2024 (UK, ultra generalised) | Office for National Statistics, December 2024 | web service | open | LAD24CD (string, 9 chars) |
| `ref.boundary.us.aiannh.cb500k` | US American Indian, Alaska Native and Native Hawaiian areas, 1:500,000 (Census cartographic boundary) | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string, 4 chars) |
| `ref.boundary.us.block-group.cb500k` | US block groups, 1:500,000 (Census cartographic boundary) | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string, 12 chars) |
| `ref.boundary.us.cbsa.cb500k` | US metropolitan and micropolitan areas, 1:500,000 (Census cartographic boundary) | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string, 5 chars) |
| `ref.boundary.us.congressional.cb500k` | US congressional districts, 119th Congress, 1:500,000 (Census cartographic boundary) | US Census Bureau, 119th Congress (2025) | downloaded on first use | public-domain | GEOID (string) |
| `ref.boundary.us.county.cb` | US counties, generalized (Census cartographic boundary) | US Census Bureau, 2025 | bundled (0.49 MB) | public-domain | GEOID (string, 5 chars) |
| `ref.boundary.us.county.cb500k` | US counties, 1:500,000 (Census cartographic boundary) | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string, 5 chars) |
| `ref.boundary.us.county.tiger` | US counties, full detail (Census TIGER/Line) | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string, 5 chars) |
| `ref.boundary.us.cousub.tiger` | US county subdivisions by state (Census TIGER/Line), the land base for detailed ground | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string, 10 chars) |
| `ref.boundary.us.dma.nielsen` | Designated Market Areas (Nielsen), user-supplied | Nielsen, as supplied | you supply the file | licensed | dma_code (string, 3 chars) |
| `ref.boundary.us.military.tiger` | US military installations (Census TIGER/Line) | US Census Bureau, 2025 | downloaded on first use | public-domain | AREAID (string) |
| `ref.boundary.us.place.cb500k` | US incorporated places and CDPs, 1:500,000 (Census cartographic boundary) | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string, 7 chars) |
| `ref.boundary.us.protected.pad-us` | Protected Areas Database of the United States (PAD-US 4.1) | US Geological Survey, Gap Analysis Project, 4.1 (2024) | web service | public-domain | Unit_Nm (string) |
| `ref.boundary.us.protected.usfs-forests` | USFS administrative forest boundaries (National Forests) | USDA Forest Service, EDW (live) | downloaded on first use | public-domain | forestname (string) |
| `ref.boundary.us.puma.tiger2025` | US Public Use Microdata Areas (2020 PUMAs), by state (TIGER/Line) | US Census Bureau, 2020 PUMAs (2025 TIGER) | downloaded on first use | public-domain | GEOID20 (string) |
| `ref.boundary.us.school-elementary.cb500k` | US school districts, elementary, 1:500,000, by state (Census cartographic boundary) | US Census Bureau, 2025 (school year 2024-2025) | downloaded on first use | public-domain | GEOID (string) |
| `ref.boundary.us.school-secondary.cb500k` | US school districts, secondary, 1:500,000, by state (Census cartographic boundary) | US Census Bureau, 2025 (school year 2024-2025) | downloaded on first use | public-domain | GEOID (string) |
| `ref.boundary.us.school-unified.cb500k` | US school districts, unified, 1:500,000, by state (Census cartographic boundary) | US Census Bureau, 2025 (school year 2024-2025) | downloaded on first use | public-domain | GEOID (string) |
| `ref.boundary.us.state-legislative-lower.cb500k` | US state legislative districts, lower chamber, 1:500,000, by state (Census cartographic boundary) | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string) |
| `ref.boundary.us.state-legislative-upper.cb500k` | US state legislative districts, upper chamber, 1:500,000, by state (Census cartographic boundary) | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string) |
| `ref.boundary.us.state.cb20m` | US states, generalized (Census cartographic boundary) | US Census Bureau, 2025 | bundled (0.15 MB) | public-domain | GEOID (string, 2 chars) |
| `ref.boundary.us.state.cb5m` | US states, moderately generalized (Census cartographic boundary, 1:5,000,000) | US Census Bureau, 2025 | bundled (0.69 MB) | public-domain | GEOID (string, 2 chars) |
| `ref.boundary.us.surface-management.blm` | BLM National Surface Management Agency (federal and state land ownership) | Bureau of Land Management, current BLM service | web service | public-domain | ADMIN_AGENCY_CODE (string) |
| `ref.boundary.us.tract.cb500k` | US census tracts, 1:500,000 (Census cartographic boundary) | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string, 11 chars) |
| `ref.boundary.us.urban-area.cb2020-500k` | US urban areas (2020 Census), 1:500,000 (cartographic boundary) | US Census Bureau, 2020 (decennial definition) | downloaded on first use | public-domain | GEOID20 (string, 5 chars) |
| `ref.boundary.us.voting-district.cb2020-500k` | US voting districts (precincts) as of the 2020 Census, 1:500,000, by state | US Census Bureau, 2020 (Census redistricting) | downloaded on first use | public-domain | GEOID20 (string) |
| `ref.boundary.us.zcta.cb2020-500k` | US ZIP Code Tabulation Areas, 2020 vintage, 1:500,000 (Census cartographic boundary) | US Census Bureau, 2020 (decennial) | downloaded on first use | public-domain | GEOID20 (string, 5 chars) |
| `ref.boundary.world.admin.cod-ab` | COD-AB administrative boundaries (OCHA, on the Humanitarian Data Exchange), by country | United Nations Office for the Coordination of Humanitarian Affairs (OCHA), with each country's contributing agency, as each country's dataset states (read at fetch) | downloaded on first use | attribution | ADM1_PCODE (string) |
| `ref.boundary.world.admin.gadm41` | GADM 4.1 (administrative areas worldwide, restricted licence) | GADM (University of California, Davis), 4.1 (July 2022) | downloaded on first use | restricted | GID_1 (string) |
| `ref.boundary.world.country.geoboundaries` | geoBoundaries (administrative boundaries worldwide) | geoBoundaries (William & Mary geoLab), current gbOpen release (CAN ADM1 represents 2023) | downloaded on first use | attribution | shapeISO (string) |
| `ref.boundary.world.country.ne10` | World countries, 10m (Natural Earth) | Natural Earth 5.1.1 | downloaded on first use | public-domain | ISO_A3_EH (string, 3 chars) |
| `ref.boundary.world.country.ne110` | World countries, 110m (Natural Earth) | Natural Earth 5.1.1 | bundled (0.15 MB) | public-domain | ISO_A3_EH (string, 3 chars) |
| `ref.boundary.world.country.ne50` | World countries, 50m (Natural Earth) | Natural Earth 5.1.1 | bundled (0.57 MB) | public-domain | ISO_A3_EH (string, 3 chars) |
| `ref.boundary.world.disputed-areas.ne50` | Disputed and breakaway areas, 50m (Natural Earth) | Natural Earth 5.1.1 | downloaded on first use | public-domain | none (string) |
| `ref.boundary.world.disputed-lines.ne50` | Disputed international boundary lines, 50m (Natural Earth) | Natural Earth 5.1.1 | downloaded on first use | public-domain | none (string) |
| `ref.boundary.world.division.overture` | Overture Maps divisions (administrative areas) | Overture Maps Foundation, 2026-10-21.0 | you supply the file | share-alike | id (string) |
| `ref.boundary.world.historical.awmc` | Ancient World Mapping Center geodata (Roman Empire extents, provinces, roads) | Ancient World Mapping Center (University of North Carolina at Chapel Hill), repository master (static layers) | downloaded on first use | share-alike | None (none) |
| `ref.boundary.world.historical.basemaps` | historical-basemaps - world political boundaries by century (A. Ourednik) | André Ourednik and contributors, repository master | downloaded on first use | share-alike | NAME (string) |
| `ref.boundary.world.historical.ohm` | OpenHistoricalMap administrative boundaries as of a date | OpenHistoricalMap, live, edited daily | web service | public-domain | ohm_id (integer) |
| `ref.boundary.world.protected.wdpa` | World Database on Protected Areas (WDPA, Protected Planet), by map window | UNEP-WCMC and IUCN, November 2026 (the service is replaced each month) | web service | restricted | site_id (string) |
| `ref.boundary.world.state.ne10` | States and provinces worldwide, 1:10,000,000 (Natural Earth admin-1) | Natural Earth, 5.1.x (naciscdn.org current file, downloaded 2026-11-04) | downloaded on first use | public-domain | iso_3166_2 (string) |
| `ref.boundary.world.state.ne10-locator` | World first-level divisions for locators and country lookup, 10m simplified (Natural Earth) | Natural Earth 5.1.1 | bundled (2.79 MB) | public-domain | iso_3166_2 (string) |
| `ref.boundary.world.state.ne50` | World first-level divisions, 50m (Natural Earth) | Natural Earth 5.1.1 | bundled (0.72 MB) | public-domain | adm1_code (string, 8 chars) |
| `ref.boundary.world.time-zones.ne10` | World time zones (Natural Earth) | Natural Earth 5.1.1 | downloaded on first use | public-domain | none (string) |
| `ref.context.uk.built-up.os-open-built-up-areas` | OS Open Built Up Areas: towns and cities of Great Britain as areas | Ordnance Survey, 2026-04 | downloaded on first use | attribution | gsscode (string) |
| `ref.context.uk.greenspace.os-open-greenspace` | OS Open Greenspace: parks, playing fields and other green spaces of Great Britain | Ordnance Survey, 2026-10 | downloaded on first use | attribution |  |
| `ref.context.uk.rivers.os-open-rivers` | OS Open Rivers: watercourses of Great Britain (Ordnance Survey) | Ordnance Survey, 2026-10 | downloaded on first use | attribution |  |
| `ref.context.us.hazard.fema-nfhl` | FEMA National Flood Hazard Layer (flood hazard zones) | FEMA, effective NFHL (continuously updated as studies take effect) | web service | public-domain |  |
| `ref.context.us.hydrography.nhdplus-hr` | National Hydrography Dataset Plus High Resolution (USGS map service) | US Geological Survey, The National Map, NHDPlus HR, data refreshed October 2022 | web service | public-domain |  |
| `ref.context.us.imagery.naip` | NAIP aerial photography, 0.3 to 1 m, the conterminous United States (USDA) | USDA Farm Production and Conservation Business Center, National Agriculture Imagery Program, through Microsoft Planetary Computer, 2010 to 2025, by state | downloaded on first use | public-domain |  |
| `ref.context.us.landcover.nlcd` | NLCD land cover (MRLC web map service) | Multi-Resolution Land Characteristics (MRLC) Consortium, USGS, NLCD 2021 (service); Annual NLCD 2024 (download) | web service | public-domain |  |
| `ref.context.us.water-area.tiger` | US area water by county (Census TIGER/Line) | US Census Bureau, 2025 | downloaded on first use | public-domain |  |
| `ref.context.us.water-linear.tiger` | US linear water by county (Census TIGER/Line) | US Census Bureau, 2025 | downloaded on first use | public-domain |  |
| `ref.context.world.building.google` | Google Open Buildings (Africa, South and Southeast Asia, Latin America and the Caribbean) | Google Research, v3 (2023) | you supply the file | attribution |  |
| `ref.context.world.building.microsoft` | Microsoft Global ML Building Footprints | Microsoft, 2026-02-03 | you supply the file | share-alike |  |
| `ref.context.world.building.overture` | Overture Maps buildings (footprints) | Overture Maps Foundation, 2026-10-21.0 | you supply the file | share-alike |  |
| `ref.context.world.coastline.ne110` | World coastline, 110m (Natural Earth) | Natural Earth 4.1.0 | bundled (0.09 MB) | public-domain |  |
| `ref.context.world.coastline.ne50` | World coastline, 50m (Natural Earth) | Natural Earth 5.1.1 | downloaded on first use | public-domain |  |
| `ref.context.world.forest.global-forest-change` | Global Forest Change 2000 to 2024 (Hansen, UMD and GLAD), 30 m | Hansen, Potapov, Moore, Hancher et al., University of Maryland (GLAD), Google, USGS and NASA, 2000 to 2024 (v1.12) | downloaded on first use | attribution |  |
| `ref.context.world.geographic-lines.ne50` | Equator, tropics, polar circles and the date line (Natural Earth) | Natural Earth 5.1.1 | downloaded on first use | public-domain |  |
| `ref.context.world.glaciers.ne50` | World glaciers and ice caps, 50m (Natural Earth) | Natural Earth 5.1.1 | downloaded on first use | public-domain |  |
| `ref.context.world.ground.overture` | Overture Maps land and water (base theme), for detailed ground anywhere | Overture Maps Foundation, 2026-10-21.0 | downloaded on first use | share-alike |  |
| `ref.context.world.imagery.landsat-c2-l2` | Landsat imagery (Collection 2 Level-2 surface reflectance), 30 m, the whole world since 1982 | US Geological Survey and NASA, through Microsoft Planetary Computer, 1982 to now | downloaded on first use | public-domain |  |
| `ref.context.world.imagery.sentinel-2-l2a` | Sentinel-2 imagery (Level-2A surface reflectance), 10 m, the whole world since 2017 | European Space Agency and the European Commission (Copernicus), through Element 84 Earth Search and Microsoft Planetary Computer, 2017 to now, a new scene every five days | downloaded on first use | attribution |  |
| `ref.context.world.lakes.ne10` | World lakes and reservoirs, 10m (Natural Earth) | Natural Earth 5.1.2 | bundled (1.26 MB) | public-domain |  |
| `ref.context.world.lakes.ne110` | World lakes, 110m (Natural Earth) | Natural Earth 5.0.0 | bundled (0.02 MB) | public-domain |  |
| `ref.context.world.lakes.ne50` | World lakes, 50m (Natural Earth) | Natural Earth 5.1.1 | bundled (0.13 MB) | public-domain |  |
| `ref.context.world.land.ne50` | World land, 50m (Natural Earth) | Natural Earth 5.1.1 | downloaded on first use | public-domain |  |
| `ref.context.world.land.osm-simplified` | OpenStreetMap simplified land polygons (osmdata.openstreetmap.de) | OpenStreetMap Data (osmdata.openstreetmap.de, FOSSGIS), daily build | downloaded on first use | share-alike |  |
| `ref.context.world.landcover.esa-worldcover` | ESA WorldCover 2021 land cover, 10 m, the whole world | European Space Agency (ESA) WorldCover consortium, led by VITO, 2021 (v200) | downloaded on first use | attribution |  |
| `ref.context.world.marine-areas.ne50` | World seas, gulfs and bays as named areas, 50m (Natural Earth) | Natural Earth 5.1.1 | downloaded on first use | public-domain |  |
| `ref.context.world.ocean.ne10` | World ocean, 10m (Natural Earth) | Natural Earth 5.1.2 | bundled (2.49 MB) | public-domain |  |
| `ref.context.world.ocean.ne50` | World ocean, 50m (Natural Earth) | Natural Earth 5.1.1 | downloaded on first use | public-domain |  |
| `ref.context.world.playas.ne50` | World playas and salt flats, 50m (Natural Earth) | Natural Earth 5.1.1 | downloaded on first use | public-domain |  |
| `ref.context.world.reefs.ne10` | World coral reefs, 10m (Natural Earth) | Natural Earth 5.1.1 | downloaded on first use | public-domain |  |
| `ref.context.world.rivers.ne10` | World rivers and lake centerlines, 10m (Natural Earth) | Natural Earth 5.1.2 | bundled (1.22 MB) | public-domain |  |
| `ref.context.world.rivers.ne50` | World rivers, 50m (Natural Earth) | Natural Earth 5.1.1 | bundled (0.38 MB) | public-domain |  |
| `ref.context.world.urban.ne10` | World urban areas, 10m (Natural Earth) | Natural Earth 5.1.2 | bundled (2.54 MB) | public-domain |  |
| `ref.crosswalk.us.county-cbsa.omb2023` | US county to metro/micro area crosswalk (OMB July 2023 delineations) | US Census Bureau, Population Division, from OMB Bulletin 23-01, OMB July 2023 | bundled (0.2 MB) | public-domain | county_geoid (string, 5 chars) |
| `ref.gazetteer.uk.place.os-open-names` | OS Open Names: place names, road names and postcodes of Great Britain | Ordnance Survey, 2026-10 | you supply the file | attribution |  |
| `ref.gazetteer.uk.postcode.os-code-point-open` | Code-Point Open: postcode unit points of Great Britain | Ordnance Survey, 2026-11 | downloaded on first use | attribution | postcode (string) |
| `ref.gazetteer.us.address-range.tiger` | US address ranges along streets, by county (TIGER/Line address features) | US Census Bureau, 2025 | downloaded on first use | public-domain |  |
| `ref.gazetteer.us.landform.gnis` | USGS GNIS named landforms (summits, buttes, canyons, ridges, valleys) | U.S. Geological Survey, refreshed October 2026 (the service's copyright text) | web service | public-domain | gaz_id (integer) |
| `ref.gazetteer.world.address.openaddresses` | OpenAddresses (open address points, by source) | OpenAddresses (Batch), rolling | you supply the file | attribution |  |
| `ref.gazetteer.world.place.geonames` | GeoNames populated places (cities15000) | GeoNames, daily dump (downloaded 2026-11-05) | downloaded on first use | attribution |  |
| `ref.gazetteer.world.place.geonames-cities1000` | GeoNames populated places (cities1000) | GeoNames, daily dump (downloaded 2026-11-20) | downloaded on first use | attribution |  |
| `ref.gazetteer.world.place.ne10` | World populated places, 10m (Natural Earth) | Natural Earth 5.1.2 | bundled (0.33 MB) | public-domain |  |
| `ref.gazetteer.world.place.ne110` | World populated places, 110m (Natural Earth) | Natural Earth 5.1.2 | bundled (0.03 MB) | public-domain |  |
| `ref.gazetteer.world.place.ne50` | World cities and towns, 50m (Natural Earth) | Natural Earth 5.1.1 | downloaded on first use | public-domain |  |
| `ref.gazetteer.world.place.overture` | Overture Maps places (points of interest) | Overture Maps Foundation, 2026-10-21.0 | you supply the file | attribution | id (string) |
| `ref.gazetteer.world.place.wof` | Who's On First gazetteer | Who's On First (Geocode Earth), not verified | you supply the file | attribution | wof:id (integer) |
| `ref.infrastructure.ca.license.ised-spectrum` | ISED spectrum licence and site data (Canada) | Innovation, Science and Economic Development Canada, monthly extracts (2026-11-03 files) | you supply the file | attribution |  |
| `ref.infrastructure.na.road.ne10` | North America roads, 10m (Natural Earth) | Natural Earth 5.1.2 | bundled (1.02 MB) | public-domain |  |
| `ref.infrastructure.uk.road.os-open-roads` | OS Open Roads: the road network of Great Britain (Ordnance Survey) | Ordnance Survey, 2026-10 | you supply the file | attribution |  |
| `ref.infrastructure.us.energy.substations` | US electric substations (OpenStreetMap; the HIFLD layer is withdrawn) | OpenStreetMap contributors (via Overpass), not verified | you supply the file | share-alike |  |
| `ref.infrastructure.us.energy.transmission-lines` | US electric power transmission lines (HIFLD layer re-hosted on GeoPlatform) | DHS HIFLD, re-hosted by the GeoPlatform Federal User Community, 2025-08-26 (data last edited) | web service | public-domain |  |
| `ref.infrastructure.us.hifld.cell-tower` | Cellular towers (FCC ULS cellular licences, HIFLD layer re-hosted on GeoPlatform) | DHS HIFLD (from FCC ULS), re-hosted by the GeoPlatform Federal User Community, 2025-08-25 (data last edited) | web service | public-domain |  |
| `ref.infrastructure.us.license.fcc-uls` | FCC Universal Licensing System (licensed locations) | FCC, weekly complete files | you supply the file | public-domain |  |
| `ref.infrastructure.us.rail.tiger` | US railroads (Census TIGER/Line) | US Census Bureau, 2025 | downloaded on first use | public-domain |  |
| `ref.infrastructure.us.road.tiger-all` | US all roads, by county (TIGER/Line) | US Census Bureau, 2025 | downloaded on first use | public-domain |  |
| `ref.infrastructure.us.road.tiger-primary` | US primary roads (Census TIGER/Line) | US Census Bureau, 2025 | downloaded on first use | public-domain |  |
| `ref.infrastructure.us.road.tiger-prisec` | US primary and secondary roads by state (Census TIGER/Line) | US Census Bureau, 2025 | downloaded on first use | public-domain |  |
| `ref.infrastructure.us.tower.fcc-asr` | FCC Antenna Structure Registration (towers) | FCC, weekly complete file | you supply the file | public-domain |  |
| `ref.infrastructure.us.tower.land-mobile` | Land mobile commercial transmission towers (FCC ULS) | FCC Universal Licensing System, weekly ULS complete file | you supply the file | public-domain |  |
| `ref.infrastructure.us.tower.microwave` | Microwave service towers (FCC ULS microwave licences) | FCC Universal Licensing System, weekly ULS complete file | you supply the file | public-domain |  |
| `ref.infrastructure.world.airport.ne10` | World airports (Natural Earth) | Natural Earth 5.1.1 | downloaded on first use | public-domain |  |
| `ref.infrastructure.world.cell.opencellid` | OpenCelliD cell tower database (crowd-sourced) | Unwired Labs (OpenCelliD project), daily export (per-MCC files refreshed daily) | downloaded on first use | share-alike | mcc,mnc,lac,cell (string) |
| `ref.infrastructure.world.osm.geofabrik` | OpenStreetMap extracts by state or country (Geofabrik) | Geofabrik GmbH (OpenStreetMap data), daily extracts | you supply the file | share-alike |  |
| `ref.infrastructure.world.port.ne10` | World sea ports (Natural Earth) | Natural Earth 5.1.1 | downloaded on first use | public-domain |  |
| `ref.infrastructure.world.rail.ne10` | World railways (Natural Earth) | Natural Earth 5.1.1 | downloaded on first use | public-domain |  |
| `ref.infrastructure.world.road.ne10` | Roads worldwide, 1:10,000,000 (Natural Earth) | Natural Earth, 5.1.x (naciscdn.org current file, downloaded 2026-11-04) | downloaded on first use | public-domain |  |
| `ref.infrastructure.world.submarine-cable.telegeography` | Submarine cable systems and landing points (TeleGeography Submarine Cable Map) | TeleGeography | downloaded on first use | licensed |  |
| `ref.public-service.ca-bc.transit.translink` | TransLink (Metro Vancouver) GTFS and GTFS-realtime | TransLink, current GTFS | web service, your own key needed (/geo-spatial-mapping:map-credentials) | restricted |  |
| `ref.public-service.ca-on.transit.octranspo` | OC Transpo (Ottawa) GTFS and GTFS-realtime | City of Ottawa, OC Transpo, current GTFS | web service, your own key needed (/geo-spatial-mapping:map-credentials) | restricted |  |
| `ref.public-service.ca-qc.transit.stm` | STM Montréal GTFS (bus and métro) | Société de transport de Montréal, current GTFS | downloaded on first use | attribution |  |
| `ref.public-service.ca.weather.eccc-geomet` | Environment and Climate Change Canada weather, climate and water (MSC GeoMet) | Environment and Climate Change Canada, Meteorological Service of Canada, live | web service | attribution |  |
| `ref.public-service.ch.transit.opentransportdata` | opentransportdata.swiss (Switzerland public transport) | SKI+ (Systemaufgaben Kundeninformation, on behalf of the Federal Office of Transport), live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | attribution |  |
| `ref.public-service.cz.weather.chmi` | CHMI Open Data (Czechia weather observations) | Czech Hydrometeorological Institute (CHMI / ČHMÚ), live | web service | attribution |  |
| `ref.public-service.de.weather.dwd` | DWD Open Data (Germany weather observations, Climate Data Center) | Deutscher Wetterdienst (DWD), live | web service | attribution |  |
| `ref.public-service.dk.mapping.dataforsyningen` | Dataforsyningen (Denmark's national map services: WMS, WMTS, WFS) | Klimadatastyrelsen (formerly SDFI / SDFE), live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | attribution |  |
| `ref.public-service.dk.weather.dmi` | DMI Open Data (Denmark weather observations) | Danish Meteorological Institute (DMI), live | web service | attribution |  |
| `ref.public-service.es.weather.aemet` | AEMET OpenData (Spain weather and climate) | Agencia Estatal de Meteorología (AEMET), live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | attribution |  |
| `ref.public-service.fi.mapping.nls` | National Land Survey of Finland open interfaces (Topographic Database, OGC API Features) | National Land Survey of Finland (Maanmittauslaitos), live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | attribution |  |
| `ref.public-service.fi.transit.digitransit` | Digitransit (Finland) routing and transit APIs | Digitransit (HSL, Fintraffic, Waltti), live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | attribution |  |
| `ref.public-service.fi.weather.fmi` | FMI Open Data (Finland weather observations) | Finnish Meteorological Institute (FMI), live | web service | attribution |  |
| `ref.public-service.fr.weather.meteo-france` | Météo-France public API (observations, forecasts, climatology) | Météo-France, live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | open |  |
| `ref.public-service.nl.weather.knmi` | KNMI Data Platform (Netherlands weather and climate) | Royal Netherlands Meteorological Institute (KNMI), live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | attribution |  |
| `ref.public-service.no.transit.entur` | Entur (Norway) national journey planner and GTFS | Entur AS, live | web service | open |  |
| `ref.public-service.no.weather.met-frost` | MET Norway Frost (observations and climate data) | Norwegian Meteorological Institute (MET Norway), live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | attribution |  |
| `ref.public-service.se.mapping.lantmateriet` | Lantmäteriet open geodata (Sweden: elevation, laser data, maps) | Lantmäteriet, live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | attribution |  |
| `ref.public-service.se.weather.smhi` | SMHI Open Data (Sweden weather observations) | Swedish Meteorological and Hydrological Institute (SMHI), live | web service | attribution |  |
| `ref.public-service.uk.coverage.ofcom-api` | Ofcom mobile coverage API (UK, by postcode) | Ofcom, live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | restricted |  |
| `ref.public-service.uk.transit.tfl` | Transport for London Unified API | Transport for London, live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | attribution |  |
| `ref.public-service.uk.weather.metoffice-datahub` | Met Office Weather DataHub | Met Office, live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | restricted |  |
| `ref.public-service.us-az.gis.azgeo` | AZGeo Data Hub and ArcGIS Server (Arizona) | AZGeo Data Hub and ArcGIS Server, live portal | web service | open |  |
| `ref.public-service.us-ca.gis.geoportal` | California State Geoportal (gis.data.ca.gov) | California State Geoportal, live portal | web service | open |  |
| `ref.public-service.us-fl.gis.fgio` | Florida Geospatial Open Data Portal (FGIO) and FGDL | Florida Geospatial Open Data Portal, live portal | web service | open |  |
| `ref.public-service.us-tx.gis.txgio` | Texas Geographic Information Office (TxGIO, formerly TNRIS) | Texas Geographic Information Office, live portal | web service | open |  |
| `ref.public-service.us-wa.gis.geo-wa` | Washington State Geospatial Open Data Portal (geo.wa.gov) | Washington State Geospatial Open Data Portal, live portal | web service | open |  |
| `ref.public-service.us-wa.transit.onebusaway-pugetsound` | OneBusAway Puget Sound API (stops, arrivals, live vehicles) | Sound Transit and King County Metro (OneBusAway Puget Sound), live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | restricted |  |
| `ref.public-service.us-wa.transit.sound-transit` | Sound Transit GTFS (Link light rail, Sounder, T Line, ST Express) | Sound Transit, current GTFS (service periods; Fall 2026 SC-Fall-2026.6 read on Day 15) | you supply the file | restricted |  |
| `ref.public-service.us.broadband.fcc-bdc-api` | FCC Broadband Data Collection public data API | Federal Communications Commission, per as-of date | web service, your own key needed (/geo-spatial-mapping:map-credentials) | public-domain |  |
| `ref.public-service.us.energy.eia-opendata` | US Energy Information Administration Open Data API (v2) | US Energy Information Administration, rolling | web service, your own key needed (/geo-spatial-mapping:map-credentials) | public-domain |  |
| `ref.public-service.us.geocoder.census` | US Census Bureau Geocoder | US Census Bureau, Public_AR_Current benchmark | web service | public-domain |  |
| `ref.public-service.us.telecom.fcc-ecfs` | FCC Electronic Comment Filing System (ECFS) public API | Federal Communications Commission, live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | public-domain |  |
| `ref.public-service.us.weather.noaa-cdo` | NOAA Climate Data Online (CDO) web services v2 | NOAA, National Centers for Environmental Information, rolling | web service, your own key needed (/geo-spatial-mapping:map-credentials) | public-domain |  |
| `ref.public-service.us.weather.nws` | NOAA National Weather Service watches, warnings and advisories | NOAA, National Weather Service, live | web service | public-domain |  |
| `ref.public-service.us.weather.nws-forecast` | NOAA National Weather Service forecast API (api.weather.gov) | NOAA, National Weather Service, live | web service | public-domain |  |
| `ref.public-service.world.climate.copernicus-cds` | Copernicus Climate Data Store (ERA5 and other climate datasets) | ECMWF for the Copernicus Climate Change Service (C3S), live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | attribution |  |
| `ref.public-service.world.earth-observation.nasa-earthdata` | NASA Earthdata (CMR search and every DAAC), one Earthdata Login token | NASA Earth Science Data and Information System (ESDIS), live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | public-domain |  |
| `ref.public-service.world.earthquake.usgs` | USGS earthquake catalog and ShakeMap services | US Geological Survey, Earthquake Hazards Program, live | you supply the file | public-domain |  |
| `ref.public-service.world.geocoder.geoapify` | Geoapify geocoding, routing and isochrones | Geoapify GmbH, live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | attribution |  |
| `ref.public-service.world.geocoder.nominatim` | Nominatim (OpenStreetMap geocoder) | OpenStreetMap Foundation, live | web service | share-alike |  |
| `ref.public-service.world.hazard.nasa-firms` | NASA FIRMS active fire detections (VIIRS, MODIS) | NASA, Earth Science Data Systems (FIRMS), live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | public-domain |  |
| `ref.public-service.world.osm.ohsome` | ohsome API (OpenStreetMap full history, HeiGIT) | HeiGIT (Heidelberg Institute for Geoinformation Technology), OpenStreetMap full history to the date in the API's metadata | web service | share-alike |  |
| `ref.public-service.world.osm.overpass` | Overpass API (OpenStreetMap queries) | OpenStreetMap community (Overpass API, overpass-api.de), live (minutely diffs) | web service | share-alike |  |
| `ref.public-service.world.platform.carto` | CARTO platform API (US East region) | CARTO, live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | restricted |  |
| `ref.public-service.world.router.ors` | openrouteservice (HeiGIT) routing and isochrones | Heidelberg Institute for Geoinformation Technology (HeiGIT), live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | share-alike |  |
| `ref.public-service.world.router.valhalla-fossgis` | FOSSGIS public Valhalla server (routing and isochrones) | FOSSGIS e.V., live | web service | share-alike |  |
| `ref.public-service.world.street-imagery.mapillary` | Mapillary street-level imagery (Graph API) | Mapillary (Meta), rolling | web service, your own key needed (/geo-spatial-mapping:map-credentials) | share-alike |  |
| `ref.public-service.world.transit.navitia` | Navitia (journey planning on open transit data, France and beyond) | Hove (formerly Kisio Digital), live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | attribution |  |
| `ref.public-service.world.transit.transitland` | Transitland (GTFS feed catalogue and API) | Interline Technologies (Transitland), live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | restricted |  |
| `ref.public-service.world.weather.open-meteo` | Open-Meteo weather forecast and history API | Open-Meteo, live | web service | restricted |  |
| `ref.public-service.world.weather.openweathermap` | OpenWeatherMap (current weather and forecast) | OpenWeather Ltd, live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | restricted |  |
| `ref.public-service.world.weather.visual-crossing` | Visual Crossing Weather (history and forecast) | Visual Crossing Corporation | web service, your own key needed (/geo-spatial-mapping:map-credentials) | restricted |  |
| `ref.public-service.world.weather.weatherapi` | WeatherAPI.com (current, forecast and history, worldwide) | WeatherAPI.com (Apilayer), live | web service, your own key needed (/geo-spatial-mapping:map-credentials) | attribution |  |
| `ref.public-service.world.wikidata` | Wikidata SPARQL query service | Wikimedia Foundation (Wikidata), live | you supply the file | public-domain |  |
| `ref.statistic.au.coverage.accc-mobile` | ACCC Mobile Infrastructure Report: coverage per operator (Australia) | Australian Competition and Consumer Commission, 2026 | downloaded on first use | attribution | h3 (string) |
| `ref.statistic.ca.coverage.wireless-data-network` | Atlas of Canada Wireless Data Network: LTE and 5G availability (Canada) | Natural Resources Canada (Atlas of Canada), data from the CRTC, 2024 | web service | open | h3 (string) |
| `ref.statistic.de.coverage.bnetza-mobile` | Bundesnetzagentur Mobilfunk-Monitoring: operators per 100 m cell (Germany) | Bundesnetzagentur, June 2026 | downloaded on first use | attribution | id (string) |
| `ref.statistic.fr.coverage.arcep-mobile` | Arcep Mon réseau mobile: theoretical coverage per operator (France) | Arcep, 2026 T2 | downloaded on first use | open | h3 (string) |
| `ref.statistic.uk.coverage.ofcom-connected-nations` | Ofcom Connected Nations: mobile coverage by local authority (UK) | Ofcom, Connected Nations 2025 | you supply the file | open | laua (string, 9 chars) |
| `ref.statistic.us.acs5.broadband` | ACS 5-year estimates, internet subscriptions (table B28002) | US Census Bureau, 2020-2024 | web service | public-domain | GEO_ID parts (string, 5 chars) |
| `ref.statistic.us.acs5.computers` | ACS 5-year estimates, computers in the household (table B28001) | US Census Bureau, 2020-2024 | web service | public-domain | GEO_ID parts (string, 5 chars) |
| `ref.statistic.us.acs5.median-income` | ACS 5-year estimates, median household income (table B19013) | US Census Bureau, 2020-2024 | web service | public-domain | GEO_ID parts (string, 5 chars) |
| `ref.statistic.us.acs5.population` | ACS 5-year estimates, total population (Census Data API) | US Census Bureau, 2020-2024 | web service, your own key needed (/geo-spatial-mapping:map-credentials) | public-domain | GEO_ID parts (string, 5 chars) |
| `ref.statistic.us.acs5.table` | ACS 5-year estimates, any table by variable (Census Data API or Summary File) | US Census Bureau, 2020-2024 | web service, your own key needed (/geo-spatial-mapping:map-credentials) | public-domain | GEO_ID parts (string, 5 chars) |
| `ref.statistic.us.broadband.bead-ntia` | NTIA BEAD final proposal locations (Section 106 public layer) | NTIA (National Broadband Availability Map organization, owner NBAM_Admin), 2026-09 (service last edited 2026-09-22) | web service | public-domain | County_GEOID (string, 5 chars) |
| `ref.statistic.us.broadband.fcc-bdc-summary` | FCC Broadband Data Collection: availability summary by geography | FCC, latest filing date on the download page (semiannual filings, June and December) | web service | public-domain | geography_id (string) |
| `ref.statistic.us.climate.noaa-normals` | NOAA U.S. Climate Normals 1991-2020 (monthly, by station) | NOAA National Centers for Environmental Information, 1991-2020 | downloaded on first use | public-domain | STATION (string, 11 chars) |
| `ref.statistic.us.coverage.fcc-bdc-mobile-h3` | FCC Broadband Data Collection: mobile coverage hexagons (H3) | FCC, latest filing date on the download page (semiannual filings, June and December) | web service | public-domain | h3_res9_id (string) |
| `ref.statistic.us.lodes.lehd` | LEHD Origin-Destination Employment Statistics (LODES) workplace counts | US Census Bureau, Center for Economic Studies (LEHD), 2022 (LODES8 on 2020 blocks) | downloaded on first use | public-domain | w_geocode (string, 15 chars) |
| `ref.statistic.us.storm-events.noaa` | NOAA Storm Events Database (bulk CSV) | NOAA National Centers for Environmental Information, 2025 events (details file created 2026-08-19); the 2026 file (created 2026-10-26) is partial | downloaded on first use | public-domain | STATE_FIPS,CZ_FIPS (string) |
| `ref.statistic.world.climate.era5-land-monthly` | ERA5-Land monthly averaged data (CDS) | ECMWF for the Copernicus Climate Change Service (C3S), monthly, updated about five days after each month | web service | attribution | none (raster) |
| `ref.statistic.world.climate.era5-monthly-single-levels` | ERA5 monthly averaged data on single levels (CDS) | ECMWF for the Copernicus Climate Change Service (C3S), monthly, updated about five days after each month | web service | attribution | none (raster) |
| `ref.statistic.world.night-lights.black-marble` | NASA Black Marble annual night lights (VNP46A4, 15 arc seconds), with an Earthdata token | NASA Goddard Space Flight Center (Black Marble team), distributed by LAADS DAAC, annual composites from 2012 | downloaded on first use | public-domain | none (raster) |
| `ref.statistic.world.population.ghsl` | GHSL population grid 2020, 100 m (GHS-POP R2023A) | European Commission, Joint Research Centre (JRC), R2023A, epoch 2020 | downloaded on first use | attribution | none (raster) |
| `ref.statistic.world.population.hrsl` | Meta High Resolution Settlement Layer (HRSL), about 30 m | Meta (Data for Good) and CIESIN, Columbia University, hrsl_general latest | downloaded on first use | attribution | none (raster) |
| `ref.statistic.world.population.kontur` | Kontur Population: people per H3 cell (resolution 8), by country | Kontur Inc., 2023-11-01 | downloaded on first use | attribution | h3 (string) |
| `ref.statistic.world.population.worldpop` | WorldPop constrained population 2020, 100 m, by country | WorldPop, University of Southampton, 2020 (the constrained series ends at 2020) | downloaded on first use | attribution | none (raster) |
| `ref.statistic.world.speed.ookla` | Ookla Speedtest open data (mobile and fixed performance tiles) | Ookla, 2025 Q3 (2025 Q4 file also listed) | downloaded on first use | restricted | quadkey (string, 16 chars) |
| `ref.terrain.antarctica.dem.rema` | REMA mosaic v2.0 (32 m), Antarctica | Polar Geospatial Center, University of Minnesota, Reference Elevation Model of Antarctica (REMA) Mosaic v2.0 | downloaded on first use | attribution |  |
| `ref.terrain.arctic.dem.arcticdem` | ArcticDEM mosaic v4.1 (32 m), the Arctic above 60 N | Polar Geospatial Center, University of Minnesota, ArcticDEM Mosaic v4.1 | downloaded on first use | attribution |  |
| `ref.terrain.au.dem.ga-lidar-5m` | Geoscience Australia 5 m LiDAR DEM (bare earth) | Geoscience Australia, DEM_LiDAR_5m_2025 | downloaded on first use | attribution |  |
| `ref.terrain.be-vlg.dem.dhmv` | Digitaal Hoogtemodel Vlaanderen II, DTM 1 m (Flanders) | Digitaal Vlaanderen, DHMV II (2013-2015) | downloaded on first use | attribution |  |
| `ref.terrain.be-vlg.lidar.dhmv-openlidar` | DHMV II LiDAR point cloud (Flanders, openlidar LAZ tiles) | Digitaal Vlaanderen (EODaS), DHMV II (2013-2015) | downloaded on first use | attribution |  |
| `ref.terrain.ca.dem.mrdem` | NRCan CanElevation MRDEM-30, Canada's 30 m elevation model (DTM) | Natural Resources Canada, MRDEM-30 (2026 update) | downloaded on first use | open |  |
| `ref.terrain.fr.dem.rge-alti` | IGN RGE ALTI, France's 1 m elevation model (bare earth) | Institut national de l'information géographique et forestière (IGN), RGE ALTI 1 m (Geoplateforme) | downloaded on first use | open |  |
| `ref.terrain.jp.dem.gsi` | GSI Japan elevation tiles (dem_png, about 10 m) | Geospatial Information Authority of Japan, dem_png (DEM10B) | downloaded on first use | attribution |  |
| `ref.terrain.se.dem.lantmateriet-mhm` | Lantmäteriet Markhöjdmodell, 1 m (Sweden, bare earth) | Lantmäteriet, Markhöjdmodell (updated continuously) | downloaded on first use | attribution |  |
| `ref.terrain.us.dem.3dep` | USGS 3DEP bare-earth elevation (ImageServer) | US Geological Survey, National Geospatial Program, all 3DEP DEM data published as of October 26, 2026 (the service description states its date) | web service | public-domain |  |
| `ref.terrain.world.bathymetry.gebco` | GEBCO 2026 grid (sea floor and land, 15 arc seconds) | GEBCO Compilation Group (IHO and IOC of UNESCO), GEBCO_2026 | downloaded on first use | public-domain |  |
| `ref.terrain.world.dem.copernicus-glo30` | Copernicus DEM GLO-30 Public (with GLO-90), world elevation, keyless from AWS Open Data | European Space Agency and the Copernicus Programme, hosted by Sinergise on the Registry of Open Data on AWS, Copernicus DEM 2021 release | downloaded on first use | attribution |  |
| `ref.terrain.world.dem.nasadem` | NASADEM (reprocessed SRTM, 1 arc second), 60 N to 56 S, with an Earthdata token | NASA JPL, distributed by the NASA Land Processes DAAC (LP DAAC), NASADEM 001 (2020), SRTM data of February 2000 | downloaded on first use | public-domain |  |
| `ref.terrain.world.dem.opentopography` | OpenTopography global DEMs (SRTM, Copernicus, ALOS, NASADEM) | OpenTopography (San Diego Supercomputer Center, UC San Diego), per dataset | downloaded on first use | attribution |  |
| `ref.terrain.world.dem.terrarium` | Terrain Tiles on AWS (Mapzen Terrarium), world elevation | Mapzen (Linux Foundation), hosted on the Registry of Open Data on AWS, joerd build of the tileset (sources listed in the attribution document; 3DEP and SRTM v3 for the US and the world between 60 N and 56 S) | downloaded on first use | attribution |  |
| `ref.terrain.world.relief.ne50-hypsometric` | World hypsometric tint with shaded relief and water, 50m (Natural Earth, a colour picture) | Natural Earth 5.1.1 | downloaded on first use | public-domain |  |
| `ref.terrain.world.relief.ne50-shaded` | World shaded relief, 50m (Natural Earth, a picture of relief) | Natural Earth 5.1.1 | downloaded on first use | public-domain |  |

### Not usable yet

These are in the library so that a request can be answered honestly, but a map cannot use them until they are verified or the terms allow it.

| Entry | What it is | Status | Why |
|---|---|---|---|
| `ref.context.uk.basemap.os-open-zoomstack` | OS Open Zoomstack: a layered base map of Great Britain (Ordnance Survey) | draft | New (5.2.0, open data portals P2.6). Draft: the Downloads API lists the GeoPackage (4,301.5 MB) and the vector tiles (2,852.7 MB) on 2026-11-25; neither was downloaded or read, so it is not offered. |
| `ref.context.world.hazard.ndh-hotspots` | Natural Disaster Hotspots: hazard frequency and distribution grids (CHRR and CIESIN, 2005; SEDAC, frozen) | draft | Fetched with the Earthdata tools and the person's one token: earthdata_search (short_name=CIESIN_CHRR_NDH_MULTIHZ_HFD, kind=granules) lists the files, earthdata_fetch (urls=<one file>) shows the plan and downloads after the person agrees, into data/earthdata/CIESIN_CHRR_NDH_MULTIHZ_HFD/. The GeoTIFF inside the zip is then a file layer (role: raster) with this entry named in its provenance. Draft until one download has been made with a real token (see the changelog). |
| `ref.context.world.urban.grump-extents` | GRUMP v1 urban extents grid, 1995 (SEDAC, frozen) | draft | Fetched with the Earthdata tools and the person's one token: earthdata_search (short_name=CIESIN_SEDAC_GRUMPv1_EXT, kind=granules) lists the files, earthdata_fetch (urls=<one file>) shows the plan and downloads after the person agrees, into data/earthdata/CIESIN_SEDAC_GRUMPv1_EXT/. The GeoTIFF inside the zip is then a file layer (role: raster) with this entry named in its provenance. Draft until one download has been made with a real token (see the changelog). |
| `ref.public-service.uk.mapping.os-ngd-features` | OS NGD API - Features: the National Geographic Database of Great Britain by collection (Ordnance Survey) | draft | The collections list (https://api.os.uk/features/ngd/ofa/v1/collections) answers with no key; items need the key, and the key works only if the NGD API - Features is added to its project in the OS Data Hub. Draft until an items request has been made with a real key (see the changelog). |
| `ref.statistic.world.population.gpw-v4-count` | Gridded Population of the World v4.11: population count, 2000 to 2020 (SEDAC, frozen) | draft | Fetched with the Earthdata tools and the person's one token: earthdata_search (short_name=CIESIN_SEDAC_GPWv4_POPCOUNT_R11, kind=granules) lists the files, earthdata_fetch (urls=<one file>) shows the plan and downloads after the person agrees, into data/earthdata/CIESIN_SEDAC_GPWv4_POPCOUNT_R11/. The GeoTIFF inside the zip is then a file layer (role: raster) with this entry named in its provenance. Draft until one download has been made with a real token (see the changelog). |
| `ref.statistic.world.population.gpw-v4-density` | Gridded Population of the World v4.11: population density, 2000 to 2020 (SEDAC, frozen) | draft | Fetched with the Earthdata tools and the person's one token: earthdata_search (short_name=CIESIN_SEDAC_GPWv4_POPDENS_R11, kind=granules) lists the files, earthdata_fetch (urls=<one file>) shows the plan and downloads after the person agrees, into data/earthdata/CIESIN_SEDAC_GPWv4_POPDENS_R11/. The GeoTIFF inside the zip is then a file layer (role: raster) with this entry named in its provenance. Draft until one download has been made with a real token (see the changelog). |
| `ref.terrain.uk.dem.os-terrain-50` | OS Terrain 50: a 50 m elevation model of Great Britain (Ordnance Survey) | draft | New (5.2.0, open data portals P2.6). Draft: the Downloads API lists the ASCII grid (161.7 MB) on 2026-11-25; it was not read, so it is not offered. |

## Sources by country (3.0.0)

Every national source is reached through a data adapter (`scripts/adapters/`) that takes the map's region and fetches by its partitions;
`catalog_search --region` ranks them by how much of the region they cover. The gotchas below are grouped the same way.

- **United States.** FCC Broadband Data Collection (the summary by geography; mobile coverage hexagons on H3 through the `fcc-bdc-mobile`
  adapter, one call per region with `fcc_bdc_coverage`), Census (ACS, TIGER, cartographic boundaries), USGS 3DEP, HIFLD, EIA, FCC ULS.
- **United Kingdom** (Ofcom Connected Nations), **France** (Arcep), **Germany** (Bundesnetzagentur), **Canada** (ISED), **Australia**
  (ACMA/ACCC): national regulators' coverage adapters, listed as candidates until their terms and access are verified.
- **Worldwide.** Natural Earth, geoBoundaries (CC BY 4.0) for admin-2, the Copernicus DEM, GHSL and WorldPop population (the default outside
  the US for population covered), OpenCelliD (share-alike), Ookla open data (non-commercial: internal maps only), OSM and Overture.

## Gotchas that cause wrong maps

### United States

- **FIPS codes are text.** `04013` is Maricopa County; read as a number it becomes `4013` and joins nothing. Read join columns as text and zero-pad (state 2, county 5, tract 11, block group 12).
- **Cartographic boundary files are clipped to the shoreline and generalized.** Use them for thematic maps. Legal boundaries and area calculations need the full-detail TIGER/Line files.
- **TIGER/Line vintage is a year, not a date.** Counties change (Connecticut replaced its counties with planning regions in 2022). Match the boundary vintage to the data vintage.
- **ZCTAs are not ZIP codes.** A ZIP Code Tabulation Area approximates the delivery area of a ZIP code; some ZIP codes have no ZCTA. Say "ZCTA" on the map.
- **Metro areas change with each OMB bulletin.** The bundled crosswalk records the delineation year; a county can move between a metro and a micro area.
- **ACS estimates carry margins of error.** Small areas (tracts, block groups) can have errors as large as the estimate. A choropleth of a small-area estimate needs the margin or a caveat.
- **Census Data API needs a key.** The plugin never asks for a key in chat. The person registers their own free key once (`/geo-spatial-mapping:map-credentials add census`,
  then pastes it into the file it names), and `ref_fetch` (the MCP server's tool on their computer) adds it at send time, with `work_folder=` the
  person's project folder so the map build finds the table there; or the person supplies a data file.
- **FCC data changes monthly** and its site may refuse automated requests. Entries stay draft until a person has verified the download.
- **HIFLD and other federal open-data portals move.** A dataset can be withdrawn or renamed; the entry's `verify_by` date says when it was last confirmed. In 2026 the HIFLD Open layers for cellular towers and transmission lines moved to the GeoPlatform Federal User Community services; microwave, land mobile and substation layers have no publisher copy, so those entries stay draft.
- **HIFLD "Cellular Towers" is not every tower.** It is the FCC ULS cellular (Part 22) licence locations, about 24,000 points. Registered structures are ASR; PCS, AWS and 5G sites are not in it.
### Worldwide

- **Big open datasets are read by area, not downloaded.** Overture (GeoParquet on S3), Ookla tiles, Microsoft footprints and the national BEAD layer hold millions of rows: filter by bbox, state or quadkey first.
- **Licences that block external use.** Ookla open data is CC BY-NC, GADM forbids redistribution, OpenCelliD is share-alike: the library marks them `restricted-license` and selection drops them for an external audience.
- **OpenStreetMap data is ODbL.** Attribution is required, and a derived database shares alike. The tile servers have their own usage policy: see `tile-licensing`.
- **Natural Earth is public domain** and needs no credit, but its political boundaries are a cartographic choice; for a client with a position on a disputed boundary, say which file you used.

## Using an entry

```bash
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT}"
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || GMAP_ROOT=$(for d in "$HOME"/.claude/plugins/cache/*/geo-spatial-mapping/* "$HOME"/.claude/plugins/marketplaces/*/geo-spatial-mapping "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-spatial-mapping* "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
  [ -f "$d/scripts/gmap_common.py" ] && echo "$(grep -o '"version": *"[0-9.]*' "$d/.claude-plugin/plugin.json" 2>/dev/null | tr -dc 0-9.) $d"; done | sort -V | tail -1 | cut -d' ' -f2-)
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-spatial-mapping: plugin root not found" >&2; false; }
GP=${USERPROFILE:-}/.geo-mapping/runtime/venv/Scripts/python.exe; if [ -f "$GP" ]; then python3(){ "$GP" "$@";}; fi
python3 "$GMAP_ROOT/scripts/ref_library.py" get ref.boundary.us.county.cb500k       # the whole entry
python3 "$GMAP_ROOT/scripts/ref_fetch.py" status ref.boundary.us.county.cb500k      # is it cached?
```

A layer takes `source: {type: ref, id: ref.boundary.us.county.cb500k}`. Downloaded entries are fetched once, hash-checked and cached in the working folder.
