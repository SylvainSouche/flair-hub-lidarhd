# Data sources, licences and attribution

All data were downloaded from the IGN Géoplateforme (data.geopf.fr) and cropped / clipped to the
extents of the FLAIR-HUB ROIs (2,822 ROIs, Lambert-93 / EPSG:2154).

| Product | Producer | Source service | Licence | Last update of the source |
|---|---|---|---|---|
| LiDAR HD point clouds (classified, COPC LAZ) | IGN | `data.geopf.fr/telechargement` (LiDARHD-NUALID) | Licence Ouverte 2.0 | per tile, `date_edition` in `metadata/flair_hub_lidar_tiles.csv` (2024-12 → 2026-04) |
| LiDAR HD MNT (DTM), 0.5 m | IGN | WMS-R `IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93` | Licence Ouverte 2.0 | idem |
| LiDAR HD MNS (DSM), 0.5 m | IGN | WMS-R `IGNF_LIDAR-HD_MNS_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93` | Licence Ouverte 2.0 | idem |
| BD ORTHO infrarouge couleur (IRC), 0.2 m — edition 2024, else most recent covering edition | IGN | WMS-R `ORTHOIMAGERY.ORTHOPHOTOS.IRC.<year>` | Licence Ouverte 2.0 | per ROI, edition in `metadata/bdortho_irc_layers.csv` and the GeoTIFF tag `SOURCE_LAYER` |
| BD France TOPO – thème végétation (beta) | IGN; aggregates OCS GE, BD TOPO, Masque Forêt-Lande (IGN) and RPG (ASP / IGN) | WFS `IGNF_BD-FRANCE-TOPO-VEGETATION:BD-FRANCE-TOPO-VEGETATION` | Licence Ouverte 2.0 | per polygon, `millesime` + `source` columns (2021–2024) |
| Corine Land Cover 2018 (FR) | EEA / Copernicus CLMS, SDES | WFS `LANDCOVER.CLC18_FR:clc18_fr` | Copernicus data policy (free, full and open) | 2018 edition |
| FLAIR-HUB ROI geometry (used to crop) | IGN | huggingface.co/datasets/IGNF/FLAIR-HUB | Licence Ouverte 2.0 | FLAIR-HUB release |

LiDAR acquisition dates range from 2021-04 to 2026-03 (`date_debut_acquisition` /
`date_fin_acquisition` per tile).

## Licence texts

- Licence Ouverte 2.0 (Etalab): [LICENCE-OUVERTE-2.0-FR.md](LICENCE-OUVERTE-2.0-FR.md) (official, French),
  [OPEN-LICENCE-2.0-EN.md](OPEN-LICENCE-2.0-EN.md) (English translation).
  Source: <https://github.com/etalab/licence-ouverte>. Compatible with CC-BY 2.0+ and ODC-BY.
- Copernicus: [COPERNICUS-CLMS.md](COPERNICUS-CLMS.md).

## Attribution notice to reuse

> Contains IGN data — LiDAR HD, BD ORTHO IRC, BD France TOPO végétation (beta), FLAIR-HUB — Licence Ouverte 2.0,
> IGN, downloaded 2026-10 from data.geopf.fr; last update dates per tile / polygon in the metadata.
> © European Union, Copernicus Land Monitoring Service 2018, European Environment Agency (EEA) —
> Corine Land Cover 2018, SDES.
> Modified: cropped / clipped to FLAIR-HUB ROI extents, rasters recompressed (DEFLATE), point clouds
> cropped and re-encoded as LAZ 1.4, orthophotos re-encoded as JPEG-compressed GeoTIFF.
