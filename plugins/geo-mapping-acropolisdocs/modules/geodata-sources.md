---
name: geodata-sources
description: >
  KNOWLEDGE MODULE - read by map-create, geo-find-data and reference-curate. What the reference library holds today (generated from the
  entries) and the gotchas of the common public sources: Census, TIGER, Natural Earth, FCC, HIFLD, OSM.
---

# geodata-sources

Read this to answer "what data do we have?" or to explain why a source behaves as it does. The tables are generated from the
library at build time, so they always match the entries; do not edit them by hand. For a ranked answer to a specific need use `geo-find-data`.

Library: 159 entries (129 active); basemap-provider 14, boundary 31, boundary-derived 2, context 16, crosswalk 1, gazetteer 5, infrastructure 15, map-type 11, palette 2, public-service 35, statistic 16, symbol-set 1, template 6, terrain 4.

### Usable now

| Entry | What it is | Publisher, vintage | How it arrives | License | Join key |
|---|---|---|---|---|---|
| `ref.boundary-derived.us.license-area.fcc` | FCC Cellular Market Areas (CMA) and other licence areas | FCC, 2016 service (CMA definitions are static) | web service | public-domain | CMA (integer) |
| `ref.boundary.europe.nuts.gisco2024` | European NUTS regions 2024 (Eurostat GISCO) | Eurostat GISCO; administrative boundaries © EuroGeographics, NUTS 2024 | downloaded on first use | restricted | NUTS_ID (string) |
| `ref.boundary.us.aiannh.cb500k` | US American Indian, Alaska Native and Native Hawaiian areas, 1:500,000 (Census cartographic boundary) | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string, 4 chars) |
| `ref.boundary.us.block-group.cb500k` | US block groups, 1:500,000 (Census cartographic boundary) | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string, 12 chars) |
| `ref.boundary.us.cbsa.cb500k` | US metropolitan and micropolitan areas, 1:500,000 (Census cartographic boundary) | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string, 5 chars) |
| `ref.boundary.us.county.cb` | US counties, generalized (Census cartographic boundary) | US Census Bureau, 2025 | bundled (0.49 MB) | public-domain | GEOID (string, 5 chars) |
| `ref.boundary.us.county.cb500k` | US counties, 1:500,000 (Census cartographic boundary) | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string, 5 chars) |
| `ref.boundary.us.county.tiger` | US counties, full detail (Census TIGER/Line) | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string, 5 chars) |
| `ref.boundary.us.cousub.tiger` | US county subdivisions by state (Census TIGER/Line), the land base for detailed ground | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string, 10 chars) |
| `ref.boundary.us.dma.nielsen` | Designated Market Areas (Nielsen), user-supplied | Nielsen, as supplied | you supply the file | licensed | dma_code (string, 3 chars) |
| `ref.boundary.us.military.tiger` | US military installations (Census TIGER/Line) | US Census Bureau, 2025 | downloaded on first use | public-domain | AREAID (string) |
| `ref.boundary.us.place.cb500k` | US incorporated places and CDPs, 1:500,000 (Census cartographic boundary) | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string, 7 chars) |
| `ref.boundary.us.state.cb20m` | US states, generalized (Census cartographic boundary) | US Census Bureau, 2025 | bundled (0.15 MB) | public-domain | GEOID (string, 2 chars) |
| `ref.boundary.us.state.cb5m` | US states, moderately generalized (Census cartographic boundary, 1:5,000,000) | US Census Bureau, 2025 | bundled (0.69 MB) | public-domain | GEOID (string, 2 chars) |
| `ref.boundary.us.surface-management.blm` | BLM National Surface Management Agency (federal and state land ownership) | Bureau of Land Management, current BLM service | web service | public-domain | ADMIN_AGENCY_CODE (string) |
| `ref.boundary.us.tract.cb500k` | US census tracts, 1:500,000 (Census cartographic boundary) | US Census Bureau, 2025 | downloaded on first use | public-domain | GEOID (string, 11 chars) |
| `ref.boundary.us.urban-area.cb2020-500k` | US urban areas (2020 Census), 1:500,000 (cartographic boundary) | US Census Bureau, 2020 (decennial definition) | downloaded on first use | public-domain | GEOID20 (string, 5 chars) |
| `ref.boundary.us.zcta.cb2020-500k` | US ZIP Code Tabulation Areas, 2020 vintage, 1:500,000 (Census cartographic boundary) | US Census Bureau, 2020 (decennial) | downloaded on first use | public-domain | GEOID20 (string, 5 chars) |
| `ref.boundary.world.admin.gadm41` | GADM 4.1 (administrative areas worldwide, restricted licence) | GADM (University of California, Davis), 4.1 (July 2022) | you supply the file | restricted | GID_1 (string) |
| `ref.boundary.world.country.geoboundaries` | geoBoundaries (administrative boundaries worldwide) | geoBoundaries (William & Mary geoLab), current gbOpen release (CAN ADM1 represents 2023) | downloaded on first use | attribution | shapeISO (string) |
| `ref.boundary.world.country.ne110` | World countries, 110m (Natural Earth) | Natural Earth 5.1.1 | bundled (0.15 MB) | public-domain | ISO_A3_EH (string, 3 chars) |
| `ref.boundary.world.division.overture` | Overture Maps divisions (administrative areas) | Overture Maps Foundation, 2026-10-21.0 | you supply the file | share-alike | id (string) |
| `ref.boundary.world.historical.ohm` | OpenHistoricalMap administrative boundaries as of a date | OpenHistoricalMap, live, edited daily | web service | public-domain | ohm_id (integer) |
| `ref.boundary.world.state.ne10` | States and provinces worldwide, 1:10,000,000 (Natural Earth admin-1) | Natural Earth, 5.1.x (naciscdn.org current file, downloaded 2026-11-04) | downloaded on first use | public-domain | iso_3166_2 (string) |
| `ref.boundary.world.state.ne10-locator` | World first-level divisions for locators and country lookup, 10m simplified (Natural Earth) | Natural Earth 5.1.1 | bundled (2.79 MB) | public-domain | iso_3166_2 (string) |
| `ref.boundary.world.state.ne50` | World first-level divisions, 50m (Natural Earth) | Natural Earth 5.1.1 | bundled (0.72 MB) | public-domain | adm1_code (string, 8 chars) |
| `ref.context.us.hazard.fema-nfhl` | FEMA National Flood Hazard Layer (flood hazard zones) | FEMA, effective NFHL (continuously updated as studies take effect) | web service | public-domain |  |
| `ref.context.us.hydrography.nhdplus-hr` | National Hydrography Dataset Plus High Resolution (USGS map service) | US Geological Survey, The National Map, NHDPlus HR, data refreshed October 2022 | web service | public-domain |  |
| `ref.context.us.landcover.nlcd` | NLCD land cover (MRLC web map service) | Multi-Resolution Land Characteristics (MRLC) Consortium, USGS, NLCD 2021 (service); Annual NLCD 2024 (download) | web service | public-domain |  |
| `ref.context.us.water-area.tiger` | US area water by county (Census TIGER/Line) | US Census Bureau, 2025 | downloaded on first use | public-domain |  |
| `ref.context.us.water-linear.tiger` | US linear water by county (Census TIGER/Line) | US Census Bureau, 2025 | downloaded on first use | public-domain |  |
| `ref.context.world.building.google` | Google Open Buildings (Africa, South and Southeast Asia, Latin America and the Caribbean) | Google Research, v3 (2023) | you supply the file | attribution |  |
| `ref.context.world.building.microsoft` | Microsoft Global ML Building Footprints | Microsoft, 2026-02-03 | you supply the file | share-alike |  |
| `ref.context.world.building.overture` | Overture Maps buildings (footprints) | Overture Maps Foundation, 2026-10-21.0 | you supply the file | share-alike |  |
| `ref.context.world.coastline.ne110` | World coastline, 110m (Natural Earth) | Natural Earth 4.1.0 | bundled (0.09 MB) | public-domain |  |
| `ref.context.world.ground.overture` | Overture Maps land and water (base theme), for detailed ground anywhere | Overture Maps Foundation, 2026-10-21.0 | downloaded on first use | share-alike |  |
| `ref.context.world.lakes.ne10` | World lakes and reservoirs, 10m (Natural Earth) | Natural Earth 5.1.2 | bundled (1.26 MB) | public-domain |  |
| `ref.context.world.lakes.ne110` | World lakes, 110m (Natural Earth) | Natural Earth 5.0.0 | bundled (0.02 MB) | public-domain |  |
| `ref.context.world.land.osm-simplified` | OpenStreetMap simplified land polygons (osmdata.openstreetmap.de) | OpenStreetMap Data (osmdata.openstreetmap.de, FOSSGIS), daily build | downloaded on first use | share-alike |  |
| `ref.context.world.ocean.ne10` | World ocean, 10m (Natural Earth) | Natural Earth 5.1.2 | bundled (2.49 MB) | public-domain |  |
| `ref.context.world.rivers.ne10` | World rivers and lake centerlines, 10m (Natural Earth) | Natural Earth 5.1.2 | bundled (1.22 MB) | public-domain |  |
| `ref.context.world.urban.ne10` | World urban areas, 10m (Natural Earth) | Natural Earth 5.1.2 | bundled (2.54 MB) | public-domain |  |
| `ref.crosswalk.us.county-cbsa.omb2023` | US county to metro/micro area crosswalk (OMB July 2023 delineations) | US Census Bureau, Population Division, from OMB Bulletin 23-01, OMB July 2023 | bundled (0.2 MB) | public-domain | county_geoid (string, 5 chars) |
| `ref.gazetteer.world.place.geonames` | GeoNames populated places (cities15000) | GeoNames, daily dump (downloaded 2026-11-05) | downloaded on first use | attribution |  |
| `ref.gazetteer.world.place.ne10` | World populated places, 10m (Natural Earth) | Natural Earth 5.1.2 | bundled (0.33 MB) | public-domain |  |
| `ref.gazetteer.world.place.ne110` | World populated places, 110m (Natural Earth) | Natural Earth 5.1.2 | bundled (0.03 MB) | public-domain |  |
| `ref.gazetteer.world.place.overture` | Overture Maps places (points of interest) | Overture Maps Foundation, 2026-10-21.0 | you supply the file | attribution | id (string) |
| `ref.infrastructure.ca.license.ised-spectrum` | ISED spectrum licence and site data (Canada) | Innovation, Science and Economic Development Canada, monthly extracts (2026-11-03 files) | you supply the file | attribution |  |
| `ref.infrastructure.na.road.ne10` | North America roads, 10m (Natural Earth) | Natural Earth 5.1.2 | bundled (1.02 MB) | public-domain |  |
| `ref.infrastructure.us.energy.transmission-lines` | US electric power transmission lines (HIFLD layer re-hosted on GeoPlatform) | DHS HIFLD, re-hosted by the GeoPlatform Federal User Community, 2025-08-26 (data last edited) | web service | public-domain |  |
| `ref.infrastructure.us.hifld.cell-tower` | Cellular towers (FCC ULS cellular licences, HIFLD layer re-hosted on GeoPlatform) | DHS HIFLD (from FCC ULS), re-hosted by the GeoPlatform Federal User Community, 2025-08-25 (data last edited) | web service | public-domain |  |
| `ref.infrastructure.us.rail.tiger` | US railroads (Census TIGER/Line) | US Census Bureau, 2025 | downloaded on first use | public-domain |  |
| `ref.infrastructure.us.road.tiger-primary` | US primary roads (Census TIGER/Line) | US Census Bureau, 2025 | downloaded on first use | public-domain |  |
| `ref.infrastructure.us.road.tiger-prisec` | US primary and secondary roads by state (Census TIGER/Line) | US Census Bureau, 2025 | downloaded on first use | public-domain |  |
| `ref.infrastructure.world.cell.opencellid` | OpenCelliD cell tower database (crowd-sourced) | Unwired Labs (OpenCelliD project), daily export (per-MCC files refreshed daily) | you supply the file | share-alike | mcc,mnc,lac,cell (string) |
| `ref.infrastructure.world.osm.geofabrik` | OpenStreetMap extracts by state or country (Geofabrik) | Geofabrik GmbH (OpenStreetMap data), daily extracts | you supply the file | share-alike |  |
| `ref.infrastructure.world.road.ne10` | Roads worldwide, 1:10,000,000 (Natural Earth) | Natural Earth, 5.1.x (naciscdn.org current file, downloaded 2026-11-04) | downloaded on first use | public-domain |  |
| `ref.public-service.ca-qc.transit.stm` | STM Montréal GTFS (bus and métro) | Société de transport de Montréal, current GTFS | downloaded on first use | attribution |  |
| `ref.public-service.ca.weather.eccc-geomet` | Environment and Climate Change Canada weather, climate and water (MSC GeoMet) | Environment and Climate Change Canada, Meteorological Service of Canada, live | web service | attribution |  |
| `ref.public-service.uk.transit.tfl` | Transport for London Unified API | Transport for London, live | web service | attribution |  |
| `ref.public-service.us-az.gis.azgeo` | AZGeo Data Hub and ArcGIS Server (Arizona) | AZGeo Data Hub and ArcGIS Server, live portal | web service | open |  |
| `ref.public-service.us-ca.gis.geoportal` | California State Geoportal (gis.data.ca.gov) | California State Geoportal, live portal | web service | open |  |
| `ref.public-service.us-fl.gis.fgio` | Florida Geospatial Open Data Portal (FGIO) and FGDL | Florida Geospatial Open Data Portal, live portal | web service | open |  |
| `ref.public-service.us-tx.gis.txgio` | Texas Geographic Information Office (TxGIO, formerly TNRIS) | Texas Geographic Information Office, live portal | web service | open |  |
| `ref.public-service.us-wa.gis.geo-wa` | Washington State Geospatial Open Data Portal (geo.wa.gov) | Washington State Geospatial Open Data Portal, live portal | web service | open |  |
| `ref.public-service.us.broadband.fcc-bdc-api` | FCC Broadband Data Collection public data API | Federal Communications Commission, per as-of date | web service, your own key needed (/geo-mapping-acropolisdocs:map-credentials) | public-domain |  |
| `ref.public-service.us.energy.eia-opendata` | US Energy Information Administration Open Data API (v2) | US Energy Information Administration, rolling | web service, your own key needed (/geo-mapping-acropolisdocs:map-credentials) | public-domain |  |
| `ref.public-service.us.geocoder.census` | US Census Bureau Geocoder | US Census Bureau, Public_AR_Current benchmark | web service | public-domain |  |
| `ref.public-service.us.weather.noaa-cdo` | NOAA Climate Data Online (CDO) web services v2 | NOAA, National Centers for Environmental Information, rolling | web service, your own key needed (/geo-mapping-acropolisdocs:map-credentials) | public-domain |  |
| `ref.public-service.us.weather.nws` | NOAA National Weather Service watches, warnings and advisories | NOAA, National Weather Service, live | web service | public-domain |  |
| `ref.public-service.us.weather.nws-forecast` | NOAA National Weather Service forecast API (api.weather.gov) | NOAA, National Weather Service, live | web service | public-domain |  |
| `ref.public-service.world.climate.copernicus-cds` | Copernicus Climate Data Store (ERA5 and other climate datasets) | ECMWF for the Copernicus Climate Change Service (C3S), live | web service, your own key needed (/geo-mapping-acropolisdocs:map-credentials) | attribution |  |
| `ref.public-service.world.earthquake.usgs` | USGS earthquake catalog and ShakeMap services | US Geological Survey, Earthquake Hazards Program, live | you supply the file | public-domain |  |
| `ref.public-service.world.geocoder.geoapify` | Geoapify geocoding, routing and isochrones | Geoapify GmbH, live | web service, your own key needed (/geo-mapping-acropolisdocs:map-credentials) | attribution |  |
| `ref.public-service.world.geocoder.nominatim` | Nominatim (OpenStreetMap geocoder) | OpenStreetMap Foundation, live | web service | share-alike |  |
| `ref.public-service.world.hazard.nasa-firms` | NASA FIRMS active fire detections (VIIRS, MODIS) | NASA, Earth Science Data Systems (FIRMS), live | web service, your own key needed (/geo-mapping-acropolisdocs:map-credentials) | public-domain |  |
| `ref.public-service.world.osm.overpass` | Overpass API (OpenStreetMap queries) | OpenStreetMap community (Overpass API, overpass-api.de), live (minutely diffs) | you supply the file | share-alike |  |
| `ref.public-service.world.router.ors` | openrouteservice (HeiGIT) routing and isochrones | Heidelberg Institute for Geoinformation Technology (HeiGIT), live | web service, your own key needed (/geo-mapping-acropolisdocs:map-credentials) | share-alike |  |
| `ref.public-service.world.router.valhalla-fossgis` | FOSSGIS public Valhalla server (routing and isochrones) | FOSSGIS e.V., live | web service | share-alike |  |
| `ref.public-service.world.street-imagery.mapillary` | Mapillary street-level imagery (Graph API) | Mapillary (Meta), rolling | web service, your own key needed (/geo-mapping-acropolisdocs:map-credentials) | share-alike |  |
| `ref.public-service.world.weather.open-meteo` | Open-Meteo weather forecast and history API | Open-Meteo, live | web service | restricted |  |
| `ref.public-service.world.weather.openweathermap` | OpenWeatherMap (current weather and forecast) | OpenWeather Ltd, live | web service, your own key needed (/geo-mapping-acropolisdocs:map-credentials) | restricted |  |
| `ref.public-service.world.wikidata` | Wikidata SPARQL query service | Wikimedia Foundation (Wikidata), live | you supply the file | public-domain |  |
| `ref.statistic.us.acs5.broadband` | ACS 5-year estimates, internet subscriptions (table B28002) | US Census Bureau, 2020-2024 | web service | public-domain | GEO_ID parts (string, 5 chars) |
| `ref.statistic.us.acs5.computers` | ACS 5-year estimates, computers in the household (table B28001) | US Census Bureau, 2020-2024 | web service | public-domain | GEO_ID parts (string, 5 chars) |
| `ref.statistic.us.acs5.median-income` | ACS 5-year estimates, median household income (table B19013) | US Census Bureau, 2020-2024 | web service | public-domain | GEO_ID parts (string, 5 chars) |
| `ref.statistic.us.acs5.population` | ACS 5-year estimates, total population (Census Data API) | US Census Bureau, 2020-2024 | web service, your own key needed (/geo-mapping-acropolisdocs:map-credentials) | public-domain | GEO_ID parts (string, 5 chars) |
| `ref.statistic.us.acs5.table` | ACS 5-year estimates, any table by variable (Census Data API or Summary File) | US Census Bureau, 2020-2024 | web service, your own key needed (/geo-mapping-acropolisdocs:map-credentials) | public-domain | GEO_ID parts (string, 5 chars) |
| `ref.statistic.us.broadband.bead-ntia` | NTIA BEAD final proposal locations (Section 106 public layer) | NTIA (National Broadband Availability Map organization, owner NBAM_Admin), 2026-09 (service last edited 2026-09-22) | web service | public-domain | County_GEOID (string, 5 chars) |
| `ref.statistic.us.broadband.fcc-bdc-summary` | FCC Broadband Data Collection: availability summary by geography | FCC, latest filing date on the download page (semiannual filings, June and December) | web service | public-domain | geography_id (string) |
| `ref.statistic.us.lodes.lehd` | LEHD Origin-Destination Employment Statistics (LODES) workplace counts | US Census Bureau, Center for Economic Studies (LEHD), 2022 (LODES8 on 2020 blocks) | downloaded on first use | public-domain | w_geocode (string, 15 chars) |
| `ref.statistic.us.storm-events.noaa` | NOAA Storm Events Database (bulk CSV) | NOAA National Centers for Environmental Information, 2025 events (details file created 2026-08-19); the 2026 file (created 2026-10-26) is partial | downloaded on first use | public-domain | STATE_FIPS,CZ_FIPS (string) |
| `ref.statistic.world.climate.era5-land-monthly` | ERA5-Land monthly averaged data (CDS) | ECMWF for the Copernicus Climate Change Service (C3S), monthly, updated about five days after each month | web service | attribution | none (raster) |
| `ref.statistic.world.climate.era5-monthly-single-levels` | ERA5 monthly averaged data on single levels (CDS) | ECMWF for the Copernicus Climate Change Service (C3S), monthly, updated about five days after each month | web service | attribution | none (raster) |
| `ref.statistic.world.population.ghsl` | GHSL population grid 2020, 100 m (GHS-POP R2023A) | European Commission, Joint Research Centre (JRC), R2023A, epoch 2020 | downloaded on first use | attribution | none (raster) |
| `ref.statistic.world.population.worldpop` | WorldPop constrained population 2020, 100 m, by country | WorldPop, University of Southampton, 2020 (the constrained series ends at 2020) | downloaded on first use | attribution | none (raster) |
| `ref.statistic.world.speed.ookla` | Ookla Speedtest open data (mobile and fixed performance tiles) | Ookla, 2025 Q3 (2025 Q4 file also listed) | you supply the file | restricted | quadkey (string, 16 chars) |
| `ref.terrain.us.dem.3dep` | USGS 3DEP bare-earth elevation (ImageServer) | US Geological Survey, National Geospatial Program, all 3DEP DEM data published as of October 26, 2026 (the service description states its date) | web service | public-domain |  |
| `ref.terrain.world.dem.copernicus-glo30` | Copernicus DEM GLO-30 Public (with GLO-90), world elevation, keyless from AWS Open Data | European Space Agency and the Copernicus Programme, hosted by Sinergise on the Registry of Open Data on AWS, Copernicus DEM 2021 release | downloaded on first use | attribution |  |
| `ref.terrain.world.dem.opentopography` | OpenTopography global DEMs (SRTM, Copernicus, ALOS, NASADEM) | OpenTopography (San Diego Supercomputer Center, UC San Diego), per dataset | downloaded on first use | attribution |  |
| `ref.terrain.world.dem.terrarium` | Terrain Tiles on AWS (Mapzen Terrarium), world elevation | Mapzen (Linux Foundation), hosted on the Registry of Open Data on AWS, joerd build of the tileset (sources listed in the attribution document; 3DEP and SRTM v3 for the US and the world between 60 N and 56 S) | downloaded on first use | attribution |  |

### Not usable yet

These are in the library so that a request can be answered honestly, but a map cannot use them until they are verified or the terms allow it.

| Entry | What it is | Status | Why |
|---|---|---|---|
| `ref.boundary-derived.us.cbrs-pal.fcc` | FCC CBRS Priority Access Licenses (Auction 105) by county | draft | In a browser open https://auctiondata.fcc.gov/public/projects/auction105 (verified 2026-11-04, HTTP 200) and download results_by_license.zip and all_county_status.zip from the page's downloads; the static_files links are served by the page's application, so a direct request returns the page, not the zip. Supply the zip; join on county FIPS to ref.boundary.us.county.cb. |
| `ref.boundary.ca.census-division.statcan2021` | Canadian census divisions (StatCan 2021 cartographic boundary file) | draft | Download lcd_000b21a_e.zip from the 2021 Census boundary files page (www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/boundary-limites/index2021-eng.cfm, 'cartographic boundary file') in a browser and supply it. www12.statcan.gc.ca answered HTTP 403 to the build environment on 2026-11-04 for the boundary file index and the zips, so the files are not verified; the Statistics Canada Open Licence page was read. |
| `ref.boundary.ca.csd.statcan2021` | Canadian census subdivisions (StatCan 2021 cartographic boundary file) | draft | Download lcsd000b21a_e.zip from the 2021 Census boundary files page (www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/boundary-limites/index2021-eng.cfm, 'cartographic boundary file') in a browser and supply it. www12.statcan.gc.ca answered HTTP 403 to the build environment on 2026-11-04 for the boundary file index and the zips, so the files are not verified; the Statistics Canada Open Licence page was read. |
| `ref.boundary.ca.province.statcan2021` | Canadian provinces and territories (StatCan 2021 cartographic boundary file) | draft | Download lpr_000b21a_e.zip from the 2021 Census boundary files page (www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/boundary-limites/index2021-eng.cfm, 'cartographic boundary file') in a browser and supply it. www12.statcan.gc.ca answered HTTP 403 to the build environment on 2026-11-04 for the boundary file index and the zips, so the files are not verified; the Statistics Canada Open Licence page was read. |
| `ref.boundary.mx.municipio.inegi2023` | Mexican municipalities (INEGI Marco Geoestadístico 2023) | draft | Open https://www.inegi.org.mx/app/biblioteca/ficha.html?upc=794551067314 (verified 2026-11-04) and download the 'conjunto de datos' archive in a browser; supply the 00mun shapefile. www.inegi.org.mx answered HTTP 403 for the download zip and the topic page from the build environment on 2026-11-04; the product record page answered 200. |
| `ref.boundary.mx.state.inegi2023` | Mexican states (INEGI Marco Geoestadístico 2023) | draft | Open https://www.inegi.org.mx/app/biblioteca/ficha.html?upc=794551067314 (verified 2026-11-04) and download the 'conjunto de datos' archive in a browser; supply the 00ent shapefile. www.inegi.org.mx answered HTTP 403 for the download zip and the topic page from the build environment on 2026-11-04; the product record page answered 200. |
| `ref.boundary.us.protected.pad-us` | Protected Areas Database of the United States (PAD-US 4.1) | draft | Open https://www.sciencebase.gov/catalog/item/652ef930d34edd15305a9b03 (PAD-US 4.1, verified 2026-11-04) and download the state or national geodatabase; clip to the market and supply it. The ArcGIS feature service address recorded in earlier notes answered 'Invalid URL', so no service is recorded. |
| `ref.gazetteer.world.place.wof` | Who's On First gazetteer | draft | See https://whosonfirst.org/docs/licenses/ (verified 2026-11-04) and the project's download page for the current distribution; the admin-us, admin-ca and admin-mx databases are the relevant ones. Supply the file. |
| `ref.infrastructure.us.energy.substations` | US electric substations (HIFLD, withdrawn from HIFLD Open) | draft | The HIFLD Open feature service that carried this layer (services1.arcgis.com/Hp6G80Pky0om7QvQ) answers 'Invalid URL' for it (2026-11-04): the layer was withdrawn from HIFLD Open. Third-party copies exist on ArcGIS Online (state agencies, universities) but none from the publisher, so nothing is recorded as a source. The EIA U.S. Energy Atlas (atlas.eia.gov) publishes a substations layer; export it there and supply the file. |
| `ref.infrastructure.us.license.fcc-uls` | FCC Universal Licensing System (licensed locations) | draft | Download the per-service 'complete' file in a browser from data.fcc.gov/download/pub/uls/complete/: l_cell.zip (cellular), l_micro.zip (microwave), l_LMcomm.zip (land mobile commercial), l_paging.zip, l_coast.zip. The LO table holds latitude and longitude in degrees, minutes, seconds with N/S and E/W flags; convert to decimal. For cellular licence locations a verified service copy exists: ref.infrastructure.us.hifld.cell-tower. |
| `ref.infrastructure.us.tower.fcc-asr` | FCC Antenna Structure Registration (towers) | draft | Download r_tower.zip from the ULS 'complete' download page at data.fcc.gov/download/pub/uls/complete/ in a browser, then supply the zip. The RA table carries registration number, status and structure height; CO carries coordinates in degrees, minutes, seconds with N/S and E/W flags: convert to decimal before mapping. Field layouts are in the ULS public access database definitions (pa_ddef) on the same page. |
| `ref.infrastructure.us.tower.land-mobile` | Land mobile commercial transmission towers (FCC ULS; HIFLD layer withdrawn) | draft | Download l_LMcomm.zip in a browser from data.fcc.gov/download/pub/uls/complete/ and supply it; see ref.infrastructure.us.license.fcc-uls for the table layout. The HIFLD Open feature service that carried this layer (services1.arcgis.com/Hp6G80Pky0om7QvQ) answers 'Invalid URL' for it (2026-11-04): the layer was withdrawn from HIFLD Open. Third-party copies exist on ArcGIS Online (state agencies, universities) but none from the publisher, so nothing is recorded as a source. |
| `ref.infrastructure.us.tower.microwave` | Microwave service towers (FCC ULS microwave licences; HIFLD layer withdrawn) | draft | Download l_micro.zip in a browser from data.fcc.gov/download/pub/uls/complete/ and supply it; see ref.infrastructure.us.license.fcc-uls for the table layout. The HIFLD Open feature service that carried this layer (services1.arcgis.com/Hp6G80Pky0om7QvQ) answers 'Invalid URL' for it (2026-11-04): the layer was withdrawn from HIFLD Open. Third-party copies exist on ArcGIS Online (state agencies, universities) but none from the publisher, so nothing is recorded as a source. |
| `ref.public-service.ca-bc.transit.translink` | TransLink (Metro Vancouver) GTFS and GTFS-realtime | draft | Register at translink.ca (app-developer-resources/register) for a key; static data at app-developer-resources/gtfs/gtfs-data and real-time at /gtfs/gtfs-realtime. Use is under TransLink's Open API Terms of Use (not read). |
| `ref.public-service.ca-on.transit.octranspo` | OC Transpo (Ottawa) GTFS and GTFS-realtime | draft | API 2.0 retired 30 April 2025. GTFS-RT for registered developers, allowed for commercial and non-commercial use (developers FAQ, read 2026-11-07). |
| `ref.public-service.es.weather.aemet` | AEMET OpenData (Spain weather and climate) | draft | Request a key with an e-mail address (Obtención de API Key on opendata.aemet.es). The station inventory endpoint answered 200 with an empty body without a key on 2026-11-07. |
| `ref.public-service.fi.transit.digitransit` | Digitransit (Finland) routing and transit APIs | draft | Registration page (read 2026-11-07): key as header or URL parameter named digitransit-subscription-key; production hosts api.digitransit.fi and cdn.digitransit.fi. Routing paths (routing/v2/<router>/gtfs/v1, GraphQL) were not confirmed; a guessed path answered 404. |
| `ref.public-service.nl.weather.knmi` | KNMI Data Platform (Netherlands weather and climate) | draft | Without a key the API answers 401 "Authorization field missing" (2026-11-07). An EDR API sits at api.dataplatform.knmi.nl/edr/v1 (not checked). |
| `ref.public-service.no.weather.met-frost` | MET Norway Frost (observations and climate data) | draft | How-to page (read 2026-11-07): register with an e-mail address for a client ID and secret; requests use the client ID as the HTTP Basic user. The API's own error response names its licence, CC BY 3.0 NO (2026-11-07). |
| `ref.public-service.uk.weather.metoffice-datahub` | Met Office Weather DataHub | draft | Register, choose a product (Global Spot site-specific, observations, map images), subscribe to a plan; API-call plans have daily limits that reset at 00:00 UTC. Product data is GeoJSON. Key delivery and the free plan were not read. |
| `ref.public-service.world.transit.transitland` | Transitland (GTFS feed catalogue and API) | draft | Without a key the API answers 401 Unauthorized (2026-11-07). |
| `ref.public-service.world.weather.visual-crossing` | Visual Crossing Weather (history and forecast) | draft | The Timeline API at weather.visualcrossing.com answered 400 without a key on 2026-11-07, asking anonymous requests to accept gzip; the key parameter was not confirmed. |
| `ref.statistic.mx.coverage.ift` | IFT mobile coverage maps (Mexico), reference only | draft | Open https://www.ift.org.mx/usuarios-y-audiencias/mapas-de-cobertura (verified 2026-11-04) to view operator coverage by technology; cite the page and date. |
| `ref.statistic.us.coverage.fcc-nbm` | FCC National Broadband Map (availability data) | draft | In a browser open broadbandmap.fcc.gov/data-download, choose the filing date, 'Summary by Geography' (or 'Availability' for location-level rows), the state and technology, and download the zip. Supply the zip; the columns are documented by the FCC's BDC data specification shipped with the download. See ref.statistic.us.broadband.fcc-bdc-summary for the summary layout. |

## Gotchas that cause wrong maps

- **FIPS codes are text.** `04013` is Maricopa County; read as a number it becomes `4013` and joins nothing. Read join columns as text and zero-pad (state 2, county 5, tract 11, block group 12).
- **Cartographic boundary files are clipped to the shoreline and generalized.** Use them for thematic maps. Legal boundaries and area calculations need the full-detail TIGER/Line files.
- **TIGER/Line vintage is a year, not a date.** Counties change (Connecticut replaced its counties with planning regions in 2022). Match the boundary vintage to the data vintage.
- **ZCTAs are not ZIP codes.** A ZIP Code Tabulation Area approximates the delivery area of a ZIP code; some ZIP codes have no ZCTA. Say "ZCTA" on the map.
- **Metro areas change with each OMB bulletin.** The bundled crosswalk records the delineation year; a county can move between a metro and a micro area.
- **ACS estimates carry margins of error.** Small areas (tracts, block groups) can have errors as large as the estimate. A choropleth of a small-area estimate needs the margin or a caveat.
- **Census Data API needs a key.** The plugin never asks for a key in chat. The person registers their own free key once (`/geo-mapping-acropolisdocs:map-credentials add census`,
  then pastes it into the file it names), and `ref_fetch` (the MCP server's tool on their computer) adds it at send time, with `work_folder=` the
  person's project folder so the map build finds the table there; or the person supplies a data file.
- **FCC data changes monthly** and its site may refuse automated requests. Entries stay draft until a person has verified the download.
- **HIFLD and other federal open-data portals move.** A dataset can be withdrawn or renamed; the entry's `verify_by` date says when it was last confirmed. In 2026 the HIFLD Open layers for cellular towers and transmission lines moved to the GeoPlatform Federal User Community services; microwave, land mobile and substation layers have no publisher copy, so those entries stay draft.
- **HIFLD "Cellular Towers" is not every tower.** It is the FCC ULS cellular (Part 22) licence locations, about 24,000 points. Registered structures are ASR; PCS, AWS and 5G sites are not in it.
- **Big open datasets are read by area, not downloaded.** Overture (GeoParquet on S3), Ookla tiles, Microsoft footprints and the national BEAD layer hold millions of rows: filter by bbox, state or quadkey first.
- **Licences that block external use.** Ookla open data is CC BY-NC, GADM forbids redistribution, OpenCelliD is share-alike: the library marks them `restricted-license` and selection drops them for an external audience.
- **OpenStreetMap data is ODbL.** Attribution is required, and a derived database shares alike. The tile servers have their own usage policy: see `tile-licensing`.
- **Natural Earth is public domain** and needs no credit, but its political boundaries are a cartographic choice; for a client with a position on a disputed boundary, say which file you used.

## Using an entry

```bash
# Resolve this plugin's root in THIS shell call. It looks only for this plugin's own marker file.
GMAP_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ ! -f "$GMAP_ROOT/scripts/gmap_common.py" ]; then
  for d in "$HOME"/.claude/plugins/cache/*/geo-mapping-acropolisdocs/* "$HOME"/.claude/plugins/marketplaces/*/geo-mapping-acropolisdocs "$HOME"/mnt/.remote-plugins/plugin_* /mnt/skills/plugin-files/geo-mapping-acropolisdocs "$HOME"/mnt/*-plugin /mnt/skills "$PWD"; do
    [ -f "$d/scripts/gmap_common.py" ] && GMAP_ROOT="$d" && break
  done
fi
[ -f "$GMAP_ROOT/scripts/gmap_common.py" ] || { echo "geo-mapping-acropolisdocs: plugin root not found (CLAUDE_PLUGIN_ROOT=${CLAUDE_PLUGIN_ROOT:-unset}); nothing was run" >&2; false; }
python3 "$GMAP_ROOT/scripts/ref_library.py" get ref.boundary.us.county.cb500k       # the whole entry
python3 "$GMAP_ROOT/scripts/ref_fetch.py" status ref.boundary.us.county.cb500k      # is it cached?
```

A layer takes `source: {type: ref, id: ref.boundary.us.county.cb500k}`. Downloaded entries are fetched once, hash-checked and cached in the working folder.
