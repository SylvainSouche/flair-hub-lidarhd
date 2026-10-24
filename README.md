# FLAIR-HUB × IGN LiDAR HD

Scripts to enrich the [FLAIR-HUB](https://huggingface.co/datasets/IGNF/FLAIR-HUB) dataset with
IGN open data cropped to its 2,822 ROIs (2,528 km², Lambert-93):

| Product | Source | Output per ROI |
|---|---|---|
| LiDAR HD point cloud | COPC LAZ tiles (1 km²) | `.laz` (LAS 1.4, point format kept from source) |
| LiDAR HD MNT (DTM) | WMS-R, native 0.5 m grid | Float32 GeoTIFF, DEFLATE |
| LiDAR HD MNS (DSM) | WMS-R, native 0.5 m grid | Float32 GeoTIFF, DEFLATE |
| MNH (CHM), optional | WMS-R, native 0.5 m grid | Float32 GeoTIFF, DEFLATE |
| BD France TOPO végétation (beta) | WFS | polygons clipped to the ROI |
| Corine Land Cover 2018 (→ 2012 → 2006 fallback) | WFS | polygons clipped to the ROI |

The resulting dataset is published separately on Hugging Face (see `package_hf.py`).

## Requirements

Python ≥ 3.9 (standard library only) and the GDAL and PDAL command-line tools:

```bash
conda env create -f environment.yml
conda activate flair-ign
```

The scripts expect the FLAIR-HUB `data/` folder (it must contain `GLOBAL_ALL_MTD.zip`, from which
ROI extents are read). Point to it with `--data` or:

```bash
export FLAIR_HUB_DATA=/path/to/FLAIR-HUB/data
```

## Pipeline

```bash
# 1. Tile index: LiDAR HD tiles covering the ROIs, with LAZ / MNT / MNS / MNH URLs,
#    point counts and LAZ sizes  -> output/
python3 lidar_urls.py

# 2. Rasters then point clouds, cropped to each ROI  -> <FLAIR-HUB>/data_lidar/
python3 download_lidar.py --products mnt mns
python3 download_lidar.py --products laz

# 3. Vector layers clipped to each ROI  -> <FLAIR-HUB>/data_vectors/
python3 download_vectors.py

# 4. Package for Hugging Face (zips per domain/product, metadata, licences)
python3 package_hf.py --dry-run
python3 package_hf.py --delete-source
```

Every step is resumable: re-running skips finished files; failures go to `failed*.txt`.

### Cropping details

- **ROI extent** = union of the ROI's FLAIR-HUB patches (`GLOBAL_ALL_MTD_GEOM.gpkg`).
- **Rasters** are requested on the native LiDAR HD 0.5 m grid (pixel edges at .25 / .75 m), with the
  ROI extent snapped outwards, so values are never resampled. The grid is offset by 0.25 m from the
  FLAIR-HUB 0.2 m grid; resample on your side if you need pixel-aligned patches. ROIs wider than
  2 km are fetched in blocks (the WMS refuses more than 5,000 px) and mosaicked.
- **Point clouds**: each 1 km COPC tile is downloaded once, cropped (PDAL `readers.copc` `bounds`)
  and merged for every ROI that touches it, then deleted. The download service allows about
  1 request/s, which makes remote COPC range reads far slower than whole-tile downloads.
- **Vectors** are clipped to the ROI extent (`ogr2ogr -clipsrc spat_extent`) and merged into one
  GeoPackage per layer with `ROI_id` and `domain` columns.

### Volumes (all 2,822 ROIs)

| | Tiles to transfer | Stored after cropping |
|---|---|---|
| LAZ | ~1.4 TB (9,976 tiles, 262 G points) | ~330 GB |
| MNT + MNS | — | 37 GB |
| Vectors | — | < 100 MB |

116 ROIs have no LiDAR HD coverage yet (listed in `output/flair_hub_lidar_summary.json`).

## Licences

- Code: MIT, see [LICENSE](LICENSE).
- Data: Licence Ouverte 2.0 (IGN) and the Copernicus data policy (Corine Land Cover); see
  [LICENSES/ATTRIBUTION.md](LICENSES/ATTRIBUTION.md) for sources, required attribution and the
  licence texts.

## Publishing the dataset

`package_hf.py` writes the Hugging Face repo folder (default: `../FLAIR-HUB-LIDARHD` next to
FLAIR-HUB). On exFAT / external drives, macOS creates `._*` files, so exclude them when uploading:

```bash
hf auth login
hf repos create <user>/FLAIR-HUB-LiDAR-HD --repo-type dataset --private
hf upload <user>/FLAIR-HUB-LiDAR-HD /path/to/FLAIR-HUB-LIDARHD . --repo-type dataset --exclude "._*" --exclude "**/._*"
```
