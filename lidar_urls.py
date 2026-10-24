#!/usr/bin/env python3
"""
Scan FLAIR-HUB ROIs and list the IGN LiDAR HD tiles (1 km x 1 km) covering them:
point clouds (COPC LAZ), MNT (DTM), MNS (DSM) and MNH (CHM) download URLs,
plus point counts and file sizes to estimate the download volume.

Source of tile URLs: IGN WFS layer IGNF_LIDAR-HD_METADONNEE:metadata (data.geopf.fr).
ROI extents: GLOBAL_ALL_MTD_GEOM.gpkg read directly inside GLOBAL_ALL_MTD.zip (needs ogr2ogr).

Outputs (in --out, default: ./output next to this script):
  flair_hub_lidar_tiles.csv      one row per LiDAR tile (urls, points, sizes, ROIs, domains)
  flair_hub_lidar_roi_tiles.csv  ROI -> tile mapping (incl. tiles with no LiDAR available)
  urls_laz.txt / urls_mnt.txt / urls_mns.txt / urls_mnh.txt   plain URL lists
  flair_hub_lidar_summary.json   totals per product and per domain

Usage:
  python3 lidar_urls.py
  python3 lidar_urls.py --domains D004 D005 --no-sizes
"""
import argparse
import csv
import io
import json
import math
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# FLAIR-HUB "data" folder (the one holding GLOBAL_ALL_MTD.zip); override with $FLAIR_HUB_DATA
FLAIR_DATA = os.environ.get("FLAIR_HUB_DATA", "/Volumes/SAMSUNG/sylvain/huggingface/FLAIR-HUB/data")
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
WFS_URL = "https://data.geopf.fr/wfs/ows"
WFS_LAYER = "IGNF_LIDAR-HD_METADONNEE:metadata"
TILE = 1000  # LiDAR HD tile size, metres (Lambert-93)
# WMS GetMap of a 2000x2000 Float32 GeoTIFF (MNT / MNS / MNH at 0.5 m) is always this size
RASTER_TILE_BYTES = 16_000_513
PRODUCTS = ("laz", "mnt", "mns", "mnh")
# data.geopf.fr answers 403 to the default Python-urllib user agent
HEADERS = {"User-Agent": "flair-hub-lidar/1.0"}


def human_bytes(num):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num) < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"


def read_rois(data_dir):
    """Return {ROI_id: (xmin, ymin, xmax, ymax)} from the global geometry metadata."""
    gpkg = f"/vsizip/{data_dir}/GLOBAL_ALL_MTD.zip/GLOBAL_ALL_MTD/GLOBAL_ALL_MTD_GEOM.gpkg"
    sql = ("SELECT ROI_id, MIN(patch_xmin) xmin, MIN(patch_ymin) ymin, "
           "MAX(patch_xmax) xmax, MAX(patch_ymax) ymax "
           "FROM GLOBAL_ALL_MTD_GEOM GROUP BY ROI_id")
    out = subprocess.run(["ogr2ogr", "-f", "CSV", "/vsistdout/", gpkg, "-dialect", "SQLite", "-sql", sql],
                         check=True, capture_output=True, text=True).stdout
    return {r["ROI_id"]: tuple(float(r[k]) for k in ("xmin", "ymin", "xmax", "ymax"))
            for r in csv.DictReader(io.StringIO(out))}


def tiles_for_bbox(xmin, ymin, xmax, ymax):
    """LiDAR HD tile keys 'XXXX-YYYY' (x of west edge, y of north edge, in km) intersecting the bbox."""
    eps = 1e-6
    xs = range(math.floor(xmin / TILE), math.floor((xmax - eps) / TILE) + 1)
    ys = range(math.floor(ymin / TILE) + 1, math.floor((ymax - eps) / TILE) + 2)
    return {f"{x:04d}-{y:04d}" for x in xs for y in ys}


def urlopen_retry(req, retries=8):
    """urlopen with back-off; data.geopf.fr rate-limits (HTTP 429) bursts of requests."""
    for attempt in range(retries):
        try:
            return urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise
            wait = e.headers.get("Retry-After")
            time.sleep(float(wait) if wait and wait.isdigit() else 5 * 2 ** attempt)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(5 * 2 ** attempt)


def http_get(url):
    with urlopen_retry(urllib.request.Request(url, headers=HEADERS)) as r:
        return r.read()


def wfs_query(bbox):
    """All metadata features intersecting bbox (EPSG:2154), following pagination."""
    feats, start, count = [], 0, 1000
    while True:
        params = {
            "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
            "TYPENAMES": WFS_LAYER, "OUTPUTFORMAT": "application/json",
            "SRSNAME": "EPSG:2154", "COUNT": count, "STARTINDEX": start,
            "BBOX": ",".join(f"{v:.2f}" for v in bbox) + ",EPSG:2154",
        }
        d = json.loads(http_get(WFS_URL + "?" + urllib.parse.urlencode(params)))
        batch = d.get("features", [])
        feats += batch
        if len(batch) < count:
            return feats
        start += count


def head_size(url):
    try:
        with urlopen_retry(urllib.request.Request(url, method="HEAD", headers=HEADERS)) as r:
            return int(r.headers.get("Content-Length") or 0)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=FLAIR_DATA, help="FLAIR-HUB data dir (contains GLOBAL_ALL_MTD.zip)")
    ap.add_argument("--out", default=OUTPUT, help="output directory")
    ap.add_argument("--domains", nargs="*", help="restrict to domains, e.g. D004 D024047")
    ap.add_argument("--no-sizes", action="store_true", help="skip HEAD requests for LAZ file sizes")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    rois = read_rois(args.data)
    if args.domains:
        rois = {k: v for k, v in rois.items() if k.split("-")[0] in args.domains}
    print(f"{len(rois)} ROIs", file=sys.stderr)

    roi_tiles = {roi: tiles_for_bbox(*bb) for roi, bb in rois.items()}
    tile_rois = defaultdict(set)
    for roi, tiles in roi_tiles.items():
        for t in tiles:
            tile_rois[t].add(roi)
    print(f"{len(tile_rois)} LiDAR HD tiles needed", file=sys.stderr)

    # Query the WFS once per ROI bbox, keep only tiles we computed (bbox queries also return neighbours)
    meta = {}

    def collect(queries, label):
        with ThreadPoolExecutor(args.workers) as ex:
            futs = {ex.submit(wfs_query, bb): key for key, bb in queries.items()}
            for i, f in enumerate(as_completed(futs), 1):
                try:
                    for feat in f.result():
                        p = feat["properties"]
                        if p["coordonnees_nw"] in tile_rois:
                            meta[p["coordonnees_nw"]] = p
                except Exception as e:
                    print(f"WFS failed for {futs[f]}: {e}", file=sys.stderr)
                if i % 200 == 0:
                    print(f"  {label} {i}/{len(queries)}", file=sys.stderr)

    collect(rois, "WFS")
    # Second pass: query each still-missing tile on its own (failed requests, or genuinely no LiDAR yet)
    missing = sorted(set(tile_rois) - set(meta))
    if missing:
        print(f"re-querying {len(missing)} missing tiles", file=sys.stderr)
        centre = {t: (int(t[:4]) * TILE + 400, (int(t[5:]) - 1) * TILE + 400) for t in missing}
        collect({t: (x, y, x + 200, y + 200) for t, (x, y) in centre.items()}, "WFS retry")
    missing = sorted(set(tile_rois) - set(meta))
    print(f"{len(meta)} tiles with LiDAR HD, {len(missing)} without", file=sys.stderr)

    sizes = {}
    if not args.no_sizes:
        with ThreadPoolExecutor(args.workers) as ex:
            futs = {ex.submit(head_size, p["url_npl"]): t for t, p in meta.items() if p.get("url_npl")}
            for i, f in enumerate(as_completed(futs), 1):
                sizes[futs[f]] = f.result()
                if i % 500 == 0:
                    print(f"  HEAD {i}/{len(futs)}", file=sys.stderr)

    os.makedirs(args.out, exist_ok=True)
    cols = ["tile", "n_points", "laz_bytes", "url_laz", "url_mnt", "url_mns", "url_mnh",
            "code_mission", "date_debut_acquisition", "date_fin_acquisition", "date_edition",
            "domains", "rois"]
    with open(os.path.join(args.out, "flair_hub_lidar_tiles.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for t in sorted(meta):
            p = meta[t]
            w.writerow([t, p.get("nombre_points"), sizes.get(t), p.get("url_npl"), p.get("url_mnt"),
                        p.get("url_mns"), p.get("url_mnh"), p.get("code_mission"),
                        p.get("date_debut_acquisition"), p.get("date_fin_acquisition"), p.get("date_edition"),
                        ";".join(sorted({r.split("-")[0] for r in tile_rois[t]})),
                        ";".join(sorted(tile_rois[t]))])

    with open(os.path.join(args.out, "flair_hub_lidar_roi_tiles.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ROI_id", "domain", "tile", "available"])
        for roi in sorted(roi_tiles):
            for t in sorted(roi_tiles[roi]):
                w.writerow([roi, roi.split("-")[0], t, int(t in meta)])

    keys = {"laz": "url_npl", "mnt": "url_mnt", "mns": "url_mns", "mnh": "url_mnh"}
    for prod, key in keys.items():
        with open(os.path.join(args.out, f"urls_{prod}.txt"), "w") as fh:
            fh.writelines(meta[t][key] + "\n" for t in sorted(meta) if meta[t].get(key))

    # Volume summary
    def totals(tiles):
        tiles = [t for t in tiles if t in meta]
        laz = sum(sizes.get(t) or 0 for t in tiles)
        raster = RASTER_TILE_BYTES * len(tiles)
        return {
            "tiles": len(tiles),
            "points": sum(meta[t].get("nombre_points") or 0 for t in tiles),
            "laz_bytes": laz if sizes else None,
            "mnt_bytes": raster, "mns_bytes": raster, "mnh_bytes": raster,
        }

    by_domain = defaultdict(set)
    for t, rs in tile_rois.items():
        for r in rs:
            by_domain[r.split("-")[0]].add(t)
    summary = {
        "rois": len(rois),
        "tiles_needed": len(tile_rois),
        "tiles_missing": missing,
        "total": totals(tile_rois),
        "by_domain": {d: totals(ts) for d, ts in sorted(by_domain.items())},
        "laz_size_failures": sorted(t for t, s in sizes.items() if s is None),
    }
    with open(os.path.join(args.out, "flair_hub_lidar_summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)

    tot = summary["total"]
    print(f"\nTiles: {tot['tiles']}  points: {tot['points']:,}")
    if tot["laz_bytes"] is not None:
        print(f"LAZ (COPC):  {human_bytes(tot['laz_bytes'])}")
    for prod in ("mnt", "mns", "mnh"):
        print(f"{prod.upper()} (0.5 m Float32 GeoTIFF, uncompressed): {human_bytes(tot[prod + '_bytes'])}")
    print(f"Outputs written to {args.out}")


if __name__ == "__main__":
    main()
