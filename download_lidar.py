#!/usr/bin/env python3
"""
Download IGN LiDAR HD data cropped to each FLAIR-HUB ROI:
  laz  point cloud cropped from the 1 km COPC tiles                    -> .laz
       (each tile is downloaded once into <out>/_tiles_cache and deleted when no ROI needs it)
  mnt  terrain model, 0.5 m Float32                                    -> .tif (deflate)
  mns  surface model, 0.5 m Float32                                    -> .tif (deflate)
  mnh  canopy height model, 0.5 m Float32 (optional)                   -> .tif (deflate)

Rasters are fetched from the IGN WMS on the native LiDAR HD 0.5 m grid (pixel edges at
.25/.75 m), with the ROI extent snapped outwards, so values are not resampled.

Layout mirrors FLAIR-HUB:
  <out>/D004-2021_LIDARHD_MNT/D004-2021_LIDARHD_MNT_AA-S1-32.tif
  <out>/D004-2021_LIDARHD_LAZ/D004-2021_LIDARHD_LAZ_AA-S1-32.laz

Needs flair_hub_lidar_tiles.csv from lidar_urls.py, plus gdal and pdal on PATH.
Resumable: existing outputs are skipped. Failures are listed in <out>/failed.txt.

Usage:
  python3 download_lidar.py                        # laz, mnt, mns for every ROI
  python3 download_lidar.py --products mnt mns --domains D004
  python3 download_lidar.py --limit 3              # quick test
"""
import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from lidar_urls import FLAIR_DATA, OUTPUT, HEADERS, read_rois, tiles_for_bbox, urlopen_retry, human_bytes

WMS_URL = "https://data.geopf.fr/wms-r"
WMS_LAYERS = {
    "mnt": "IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93",
    "mns": "IGNF_LIDAR-HD_MNS_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93",
    "mnh": "IGNF_LIDAR-HD_MNH_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93",
}
RES = 0.5
GRID_OFFSET = 0.25   # LiDAR HD raster pixel edges sit at xxx.25 / xxx.75
MAX_PX = 4000        # WMS refuses WIDTH/HEIGHT above 5000
GTIFF_OPTS = ["-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=3", "-co", "TILED=YES", "-co", "BIGTIFF=IF_SAFER"]


def snap_bbox(xmin, ymin, xmax, ymax):
    lo = lambda v: math.floor((v - GRID_OFFSET) / RES) * RES + GRID_OFFSET
    hi = lambda v: math.ceil((v - GRID_OFFSET) / RES) * RES + GRID_OFFSET
    return lo(xmin), lo(ymin), hi(xmax), hi(ymax)


def out_path(out, roi, prod):
    dom_year, name = roi.split("_", 1)            # D004-2021, AA-S1-32
    ext = "laz" if prod == "laz" else "tif"
    tag = f"{dom_year}_LIDARHD_{prod.upper()}"
    return os.path.join(out, tag, f"{tag}_{name}.{ext}")


def fetch_raster(prod, bbox, dest):
    xmin, ymin, xmax, ymax = snap_bbox(*bbox)
    step = MAX_PX * RES
    with tempfile.TemporaryDirectory(dir=os.path.dirname(dest)) as tmp:
        blocks = []
        y = ymin
        while y < ymax - 1e-6:
            y2 = min(y + step, ymax)
            x = xmin
            while x < xmax - 1e-6:
                x2 = min(x + step, xmax)
                params = {
                    "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "EXCEPTIONS": "text/xml",
                    "LAYERS": WMS_LAYERS[prod], "STYLES": "", "FORMAT": "image/geotiff", "CRS": "EPSG:2154",
                    "BBOX": f"{x},{y},{x2},{y2}",
                    "WIDTH": round((x2 - x) / RES), "HEIGHT": round((y2 - y) / RES),
                }
                with urlopen_retry(urllib.request.Request(WMS_URL + "?" + urllib.parse.urlencode(params),
                                                          headers=HEADERS)) as r:
                    if "tiff" not in r.headers.get("Content-Type", ""):
                        raise RuntimeError(f"WMS error: {r.read(500)!r}")
                    path = os.path.join(tmp, f"b{len(blocks)}.tif")
                    with open(path, "wb") as fh:
                        shutil.copyfileobj(r, fh)
                blocks.append(path)
                x = x2
            y = y2
        vrt = os.path.join(tmp, "m.vrt")
        subprocess.run(["gdalbuildvrt", "-q", vrt, *blocks], check=True)
        part = dest + ".part.tif"
        subprocess.run(["gdal_translate", "-q", "-a_srs", "EPSG:2154", *GTIFF_OPTS, vrt, part], check=True)
        os.replace(part, dest)


class TileCache:
    """Full COPC tiles downloaded once, shared by the ROIs that need them, deleted after the last one.

    data.geopf.fr/telechargement allows ~1 request/s, so cropping remotely (one range request per
    COPC node) is far slower than fetching each whole tile in a single request and cropping locally.
    """
    MIN_INTERVAL = 1.2   # seconds between download requests (server limit: 1/s)

    def __init__(self, cache_dir, urls, refs):
        self.dir, self.urls, self.refs = cache_dir, urls, refs
        self.lock = threading.Lock()
        self.tile_locks = {t: threading.Lock() for t in refs}
        self.rate_lock, self.last = threading.Lock(), 0.0
        os.makedirs(cache_dir, exist_ok=True)

    def _throttle(self):
        with self.rate_lock:
            wait = self.last + self.MIN_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()

    def get(self, tile):
        path = os.path.join(self.dir, f"{tile}.copc.laz")
        with self.tile_locks[tile]:
            if not os.path.exists(path):
                self._throttle()
                with urlopen_retry(urllib.request.Request(self.urls[tile], headers=HEADERS)) as r:
                    with open(path + ".part", "wb") as fh:
                        shutil.copyfileobj(r, fh, 1 << 20)
                os.replace(path + ".part", path)
        return path

    def release(self, tile):
        with self.lock:
            self.refs[tile] -= 1
            if self.refs[tile] == 0:
                try:
                    os.remove(os.path.join(self.dir, f"{tile}.copc.laz"))
                except FileNotFoundError:
                    pass


def fetch_laz(bbox, tiles, cache, dest):
    xmin, ymin, xmax, ymax = bbox
    bounds = f"([{xmin},{xmax}],[{ymin},{ymax}])"
    part = dest + ".part.laz"
    try:
        pipeline = [{"type": "readers.copc", "filename": cache.get(t), "bounds": bounds} for t in tiles]
        if len(tiles) > 1:
            pipeline.append({"type": "filters.merge"})
        # forward=all keeps the source LAS version / point format (6 today, 8 if IGN adds RGB+NIR)
        pipeline.append({"type": "writers.las", "filename": part, "compression": True,
                         "forward": "all", "extra_dims": "all"})
        p = subprocess.run(["pdal", "pipeline", "--stdin"], input=json.dumps(pipeline),
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip()[-500:])
        os.replace(part, dest)
    finally:
        for t in tiles:
            cache.release(t)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=FLAIR_DATA, help="FLAIR-HUB data dir (contains GLOBAL_ALL_MTD.zip)")
    ap.add_argument("--tiles", default=os.path.join(OUTPUT, "flair_hub_lidar_tiles.csv"))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(FLAIR_DATA), "data_lidar"))
    ap.add_argument("--products", nargs="+", default=["laz", "mnt", "mns"], choices=["laz", "mnt", "mns", "mnh"])
    ap.add_argument("--domains", nargs="*", help="restrict to domains, e.g. D004 D024047")
    ap.add_argument("--rois", nargs="*", help="restrict to ROI ids")
    ap.add_argument("--limit", type=int, help="only the first N ROIs (testing)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--min-free-gb", type=float, default=20, help="skip LAZ jobs below this free disk space")
    args = ap.parse_args()

    with open(args.tiles) as fh:
        laz_url = {r["tile"]: r["url_laz"] for r in csv.DictReader(fh) if r["url_laz"]}

    rois = read_rois(args.data)
    if args.domains:
        rois = {k: v for k, v in rois.items() if k.split("-")[0] in args.domains}
    if args.rois:
        rois = {k: v for k, v in rois.items() if k in args.rois}
    roi_ids = sorted(rois)[: args.limit] if args.limit else sorted(rois)

    jobs = []
    for roi in roi_ids:
        for prod in args.products:
            dest = out_path(args.out, roi, prod)
            if not os.path.exists(dest):
                jobs.append((roi, prod, dest))
    print(f"{len(roi_ids)} ROIs, {len(jobs)} files to download -> {args.out}", file=sys.stderr)

    roi_laz_tiles = {roi: sorted(t for t in tiles_for_bbox(*rois[roi]) if t in laz_url)
                     for roi, prod, _ in jobs if prod == "laz"}
    refs = defaultdict(int)
    for tiles in roi_laz_tiles.values():
        for t in tiles:
            refs[t] += 1
    cache = TileCache(os.path.join(args.out, "_tiles_cache"), laz_url, refs)

    def run(roi, prod, dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        bbox = rois[roi]
        if prod == "laz":
            if shutil.disk_usage(args.out).free < args.min_free_gb * 1e9:
                raise RuntimeError(f"less than {args.min_free_gb} GB free, skipped")
            if not roi_laz_tiles[roi]:
                raise RuntimeError("no LiDAR HD tile available")
            fetch_laz(bbox, roi_laz_tiles[roi], cache, dest)
        else:
            fetch_raster(prod, bbox, dest)
        return os.path.getsize(dest)

    failed, total, t0 = [], 0, time.time()
    with ThreadPoolExecutor(args.workers) as ex:
        futs = {ex.submit(run, *j): j for j in jobs}
        for i, f in enumerate(as_completed(futs), 1):
            roi, prod, _ = futs[f]
            try:
                total += f.result()
            except Exception as e:
                failed.append(f"{roi}\t{prod}\t{e}")
                print(f"FAILED {roi} {prod}: {e}", file=sys.stderr)
            if i % 20 == 0 or i == len(jobs):
                print(f"  {i}/{len(jobs)}  {human_bytes(total)}  {(time.time() - t0) / 60:.0f} min", file=sys.stderr)

    if failed:
        with open(os.path.join(args.out, "failed.txt"), "a") as fh:
            fh.write("\n".join(failed) + "\n")
    print(f"done: {len(jobs) - len(failed)} ok, {len(failed)} failed, {human_bytes(total)} written")


if __name__ == "__main__":
    main()
