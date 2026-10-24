#!/usr/bin/env python3
"""
Download IGN WFS vector layers clipped to each FLAIR-HUB ROI and merge them into one
GeoPackage per layer, with a ROI_id column:
  vegetation  IGNF_BD-FRANCE-TOPO-VEGETATION:BD-FRANCE-TOPO-VEGETATION  (beta, partial coverage)
  clc18       LANDCOVER.CLC18_FR:clc18_fr                               (Corine Land Cover 2018)
              -> clc12, then clc06, for ROIs where the newer edition returns nothing

Outputs:
  <out>/FLAIR-HUB_VEGETATION.gpkg, <out>/FLAIR-HUB_CLC18.gpkg
  <out>/_roi/<layer>/<ROI_id>.gpkg   per-ROI pieces (resumable cache; empty ROIs get no file)
  <out>/failed_vectors.txt

Usage:
  python3 download_vectors.py
  python3 download_vectors.py --layers clc18 --domains D004
"""
import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from lidar_urls import FLAIR_DATA, read_rois

LAYERS = {
    "vegetation": "IGNF_BD-FRANCE-TOPO-VEGETATION:BD-FRANCE-TOPO-VEGETATION",
    "clc18": "LANDCOVER.CLC18_FR:clc18_fr",
    "clc12": "LANDCOVER.CLC12_FR:clc12_fr",
    "clc06": "LANDCOVER.CLC06_FR:clc06_fr",
}
# Older CLC editions fetched only for ROIs that the previous one left empty
CLC_FALLBACK = {"clc18": "clc12", "clc12": "clc06"}
WFS = "WFS:https://data.geopf.fr/wfs/ows"
GDAL_ENV = {
    "GDAL_HTTP_USERAGENT": "flair-hub-lidar/1.0",   # data.geopf.fr rejects some default agents
    "GDAL_HTTP_MAX_RETRY": "8",                     # retries 429 / 5xx
    "GDAL_HTTP_RETRY_DELAY": "5",
}


def fetch(layer, bbox, dest):
    """Clip one layer to bbox into dest. Returns feature count (0 -> no file kept)."""
    part = dest + ".part.gpkg"
    if os.path.exists(part):
        os.remove(part)
    xmin, ymin, xmax, ymax = bbox
    p = subprocess.run(
        ["ogr2ogr", "-f", "GPKG", part, WFS, LAYERS[layer], "-nln", layer,
         "-spat", str(xmin), str(ymin), str(xmax), str(ymax), "-spat_srs", "EPSG:2154",
         "-clipsrc", "spat_extent", "-nlt", "PROMOTE_TO_MULTI", "-skipfailures"],
        capture_output=True, text=True, env={**os.environ, **GDAL_ENV})
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[-400:])
    if not os.path.exists(part):        # nothing intersects: ogr2ogr writes no file
        open(dest + ".empty", "w").close()
        return 0
    info = subprocess.run(["ogrinfo", "-ro", "-so", part, layer], capture_output=True, text=True).stdout
    n = int(next((l.split(":")[1] for l in info.splitlines() if l.startswith("Feature Count")), "0"))
    if n:
        os.replace(part, dest)
    else:
        os.remove(part)
        open(dest + ".empty", "w").close()
    return n


def merge(layer, pieces, final):
    tmp = final + ".part.gpkg"
    if os.path.exists(tmp):
        os.remove(tmp)
    for i, (roi, path) in enumerate(sorted(pieces)):
        sql = f"SELECT *, '{roi}' AS ROI_id, '{roi.split('-')[0]}' AS domain FROM \"{layer}\""
        subprocess.run(["ogr2ogr", "-f", "GPKG", tmp, path, "-dialect", "SQLite", "-sql", sql,
                        "-nln", layer, "-nlt", "PROMOTE_TO_MULTI", *(["-append"] if i else [])],
                       check=True, capture_output=True)
    os.replace(tmp, final)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=FLAIR_DATA, help="FLAIR-HUB data dir (contains GLOBAL_ALL_MTD.zip)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(FLAIR_DATA), "data_vectors"))
    ap.add_argument("--layers", nargs="+", default=["vegetation", "clc18"], choices=list(LAYERS))
    ap.add_argument("--domains", nargs="*", help="restrict to domains, e.g. D004 D024047")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    rois = read_rois(args.data)
    if args.domains:
        rois = {k: v for k, v in rois.items() if k.split("-")[0] in args.domains}

    failed, t0 = [], time.time()
    piece = lambda layer, roi: os.path.join(args.out, "_roi", layer, f"{roi}.gpkg")

    def run(layer_rois):
        jobs = []
        for layer, roi_ids in layer_rois.items():
            os.makedirs(os.path.join(args.out, "_roi", layer), exist_ok=True)
            for roi in sorted(roi_ids):
                dest = piece(layer, roi)
                if not (os.path.exists(dest) or os.path.exists(dest + ".empty")):
                    jobs.append((layer, roi, dest))
        print(f"{len(jobs)} layer/ROI requests ({', '.join(layer_rois)}) -> {args.out}", file=sys.stderr)
        with ThreadPoolExecutor(args.workers) as ex:
            futs = {ex.submit(fetch, layer, rois[roi], dest): (layer, roi) for layer, roi, dest in jobs}
            for i, f in enumerate(as_completed(futs), 1):
                layer, roi = futs[f]
                try:
                    f.result()
                except Exception as e:
                    failed.append(f"{roi}\t{layer}\t{e}")
                    print(f"FAILED {roi} {layer}: {e}", file=sys.stderr)
                if i % 100 == 0 or i == len(jobs):
                    print(f"  {i}/{len(jobs)}  {(time.time() - t0) / 60:.0f} min", file=sys.stderr)

    print(f"{len(rois)} ROIs", file=sys.stderr)
    run({layer: rois for layer in args.layers})
    layers = list(args.layers)
    layer = "clc18" if "clc18" in layers else None
    while layer in CLC_FALLBACK:
        empty = [r for r in rois if os.path.exists(piece(layer, r) + ".empty")]
        if not empty:
            break
        layer = CLC_FALLBACK[layer]
        layers.append(layer)
        run({layer: empty})
    if failed:
        with open(os.path.join(args.out, "failed_vectors.txt"), "a") as fh:
            fh.write("\n".join(failed) + "\n")

    for layer in layers:
        d = os.path.join(args.out, "_roi", layer)
        pieces = [(f[:-5], os.path.join(d, f)) for f in os.listdir(d)
                  if f.endswith(".gpkg") and not f.endswith(".part.gpkg") and f[:-5] in rois]
        final = os.path.join(args.out, f"FLAIR-HUB_{layer.upper()}.gpkg")
        if pieces:
            merge(layer, pieces, final)
        print(f"{layer}: {len(pieces)}/{len(rois)} ROIs with features -> {final if pieces else '(none)'}")
    print(f"{len(failed)} failed requests")


if __name__ == "__main__":
    main()
