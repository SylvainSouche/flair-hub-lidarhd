# OpenStreetMap data — ODbL 1.0

`GLOBAL_OSM.zip` (`FLAIR-HUB_OSM_LANDCOVER.gpkg`, `FLAIR-HUB_OSM_LINES.gpkg`, `osm_coverage.csv`)
contains data extracted from OpenStreetMap with the Overpass API, clipped to the FLAIR-HUB ROIs.

**© OpenStreetMap contributors.** Available under the
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/), full text in
[ODbL-1.0.md](ODbL-1.0.md) and inside the zip. See also <https://www.openstreetmap.org/copyright>.

## What this means here (summary — the licence text is authoritative, this is not legal advice)

- **Redistribution, including for machine-learning training, is allowed**, provided the
  attribution above is kept and this extract stays under ODbL 1.0. That is why it is shipped as a
  separate zip with its own licence, apart from the IGN (Licence Ouverte 2.0) and Copernicus layers.
  Combining it with them in this dataset is a "collective database": the other layers keep their
  own licences.
- **Share-alike**: if you publicly distribute a database *derived* from these OSM layers (for example
  OSM polygons merged with other land-cover sources into a new database), that derived database must
  be offered under ODbL as well.
- **Produced works** (maps, images, statistics) made from the data need an attribution such as
  "© OpenStreetMap contributors", but may be under any licence. Whether a trained model is a produced
  work or a derived database is not settled; credit OpenStreetMap in any case, and see the OSMF
  guidance on <https://osmfoundation.org/wiki/Licence>.
- **Date**: extracted in 2026-10 from the live OSM database (current state at the time of
  extraction, which usually differs from the imagery dates).
