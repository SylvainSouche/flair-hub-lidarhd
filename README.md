# FLAIR-HUB × IGN LiDAR HD

> 🚧 **Work in progress.** The data pipeline below works and is running. The classification and
> map-rendering stages are planned.

## Why

The goal is to **render topographic maps of near-cartographic quality automatically**, closer to the
IGN *SCAN 25* or swisstopo national maps than to OpenStreetMap-based renderings such as
OpenTopoMap.

What sets those maps apart is mostly what sits under the contour lines: precise land-cover
boundaries (forest edges, scrub, rock, scree, wetlands, orchards, vineyards), coherent
generalisation, and relief shading derived from a good terrain model. Corine Land Cover gives the
right nomenclature, but its 25 ha minimum mapping unit is far too coarse for 1:25,000 maps.
OpenStreetMap land use is detailed but uneven.

The long-run plan is therefore:

1. **Data** (this repository): build an aligned, multimodal training set over the FLAIR-HUB areas.
   It combines very-high-resolution aerial imagery (FLAIR-HUB), infrared orthophotos, LiDAR HD point
   clouds and terrain / surface models, with Corine Land Cover and IGN vegetation polygons as targets.
2. **Classification**: train a model that predicts a **Corine-compatible land-cover map at
   metre-scale resolution**, using the LiDAR height structure (canopy, scrub, bare rock) and the
   near-infrared band to separate classes that RGB alone confuses.
3. **Rendering**: run that model over large areas and feed the result, with LiDAR-derived relief,
   into a cartographic style inspired by IGN and swisstopo maps.

The dataset produced here is published on Hugging Face:
**[SylvainSouche/FLAIR-HUB-LiDAR-HD](https://huggingface.co/datasets/SylvainSouche/FLAIR-HUB-LiDAR-HD)**.

## What the scripts do

They enrich every one of the 2,822 [FLAIR-HUB](https://huggingface.co/datasets/IGNF/FLAIR-HUB) ROIs
(2,528 km² across 74 French domains, Lambert-93) with IGN open data, cropped to the ROI:

| Product | Source | Output per ROI |
|---|---|---|
| LiDAR HD point cloud (classified) | 1 km COPC LAZ tiles | `.laz` (LAS 1.4, point format kept from source) |
| LiDAR HD MNT (terrain model) | WMS-R, native 0.5 m grid | Float32 GeoTIFF, DEFLATE |
| LiDAR HD MNS (surface model) | WMS-R, native 0.5 m grid | Float32 GeoTIFF, DEFLATE |
| MNH (canopy height), optional | WMS-R, native 0.5 m grid | Float32 GeoTIFF, DEFLATE |
| BD ORTHO IRC (NIR-R-G), 2024 or latest covering edition | WMS-R, 0.2 m FLAIR-HUB grid | Byte GeoTIFF, JPEG q95 |
| Corine Land Cover 2018 (→ 2012 → 2006 fallback) | WFS | polygons clipped to the ROI |
| BD France TOPO végétation (IGN beta, partial coverage) | WFS | polygons clipped to the ROI |

| Script | Role |
|---|---|
| `lidar_urls.py` | Index of the LiDAR HD tiles covering the ROIs: download URLs, point counts, sizes, dates |
| `download_lidar.py` | Point clouds, MNT / MNS / MNH and IRC orthophotos cropped to each ROI |
| `download_vectors.py` | Corine Land Cover and vegetation polygons clipped to each ROI |
| `package_hf.py` | Zips per domain and product (FLAIR-HUB layout), metadata and licences for Hugging Face |
| `upload_hf.sh` | Uploads the packaged folder to the Hub |

## Quick start

Requirements: Python ≥ 3.9 (standard library only) plus the GDAL and PDAL command-line tools.

```bash
conda env create -f environment.yml
conda activate flair-ign
```

Download FLAIR-HUB first, at least `data/GLOBAL_ALL_MTD.zip`, from which ROI extents are read. Then
point the scripts at it:

```bash
export FLAIR_HUB_DATA=/path/to/FLAIR-HUB/data
```

```bash
# 1. Tile index -> output/
python3 lidar_urls.py

# 2. Rasters, orthophotos, point clouds -> $FLAIR_HUB_DATA/../data_lidar/
python3 download_lidar.py --products mnt mns
python3 download_lidar.py --products irc
python3 download_lidar.py --products laz

# 3. Vector layers -> $FLAIR_HUB_DATA/../data_vectors/
python3 download_vectors.py

# 4. Package for Hugging Face
python3 package_hf.py --dry-run
python3 package_hf.py --delete-source
```

Every step is resumable: re-running skips finished files, and failures are listed in `failed*.txt`.
Use `--domains D004 D013` or `--limit 3` to try a subset.

## How the data are cropped

- **ROI extent**: union of the ROI's FLAIR-HUB patches (`GLOBAL_ALL_MTD_GEOM.gpkg`).
- **MNT / MNS** are requested on the native LiDAR HD 0.5 m grid (pixel edges at .25 / .75 m), with the
  ROI extent snapped outwards, so values are never resampled. This grid is offset by 0.25 m from
  the FLAIR-HUB 0.2 m grid. ROIs wider than 2 km are fetched in blocks and mosaicked, because the WMS
  caps requests at 5,000 px.
- **IRC orthophotos** are requested at 0.2 m on the exact ROI extent, pixel-aligned with the FLAIR-HUB
  patches. BD ORTHO is re-flown by department every ~3 years, so each ROI uses the 2024 edition when it
  covers the whole ROI, else the most recent covering one (2023 → 2020). The edition is logged in
  `bdortho_irc_layers.csv` and in the GeoTIFF `SOURCE_LAYER` tag. IGN's WMS reprojects BD ORTHO from
  a Web-Mercator pyramid, so pixels are resampled once by the server.
- **Point clouds**: each 1 km COPC tile is downloaded once, cropped and merged with PDAL for every ROI
  that touches it, then deleted. IGN's download service allows about 1 request/s, which makes
  remote COPC range reads much slower than whole-tile downloads.
- **Vectors** are clipped to the ROI extent and merged into one GeoPackage per layer, with `ROI_id`
  and `domain` columns.

### Volumes (all 2,822 ROIs)

| | Transferred | Stored after cropping |
|---|---|---|
| LAZ | ~1.4 TB (9,976 tiles, 262 G points) | ~330 GB |
| MNT + MNS | ~40 GB | 37 GB |
| IRC orthophotos | ~190 GB | ~21 GB |
| Vectors | — | ~30 MB |

116 ROIs have no LiDAR HD coverage yet; they are listed in `output/flair_hub_lidar_summary.json`.

## Roadmap

- [x] LiDAR HD tile index for the FLAIR-HUB ROIs
- [x] MNT / MNS per ROI
- [x] Corine Land Cover 2018 and IGN vegetation polygons per ROI
- [ ] IRC orthophotos per ROI (running)
- [ ] LiDAR point clouds per ROI (running, ~1 day)
- [ ] Hugging Face upload of every product
- [ ] Training targets: Corine classes rasterised on the FLAIR-HUB grid, reconciled with FLAIR-HUB labels
- [ ] Land-cover classification model (imagery + NIR + LiDAR height)
- [ ] Cartographic rendering pipeline (relief shading, land-cover generalisation, IGN / swisstopo-like style)

## Licences

- **Code**: MIT, see [LICENSE](LICENSE).
- **Data**:
  - IGN products (LiDAR HD, BD ORTHO, BD France TOPO, FLAIR-HUB) are under Licence Ouverte 2.0.
  - Corine Land Cover is under the Copernicus data policy.
  - Sources, required attribution and the licence texts are in [LICENSES/ATTRIBUTION.md](LICENSES/ATTRIBUTION.md).

## Publishing

```bash
hf auth login
python3 package_hf.py --products MNT MNS      # or no --products for everything complete
./upload_hf.sh                                # dot_clean + hf upload, skips unchanged files
```

`upload_hf.sh` removes the `._*` files macOS writes on exFAT / external drives and excludes
partially written zips.
