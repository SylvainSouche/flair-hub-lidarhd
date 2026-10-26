#!/usr/bin/env python3
"""
Package downloaded IGN data into the Hugging Face dataset repo, mirroring FLAIR-HUB's layout:

  <hf>/data/D004-2021_LIDARHD_MNT.zip
        D004-2021_LIDARHD_MNT/AA-S1-32/D004-2021_LIDARHD_MNT_AA-S1-32.tif
  <hf>/data/D004-2021_LIDARHD_LAZ.zip     (same for MNS)
  <hf>/data/D004-2021_BDORTHO_IRC.zip
  <hf>/data/GLOBAL_VECTORS.zip            FLAIR-HUB_CLC18.gpkg, FLAIR-HUB_VEGETATION.gpkg, ...
  <hf>/data/GLOBAL_OSM.zip                OpenStreetMap land cover + lines (ODbL), osm_coverage.csv
  <hf>/metadata/                          tile index, ROI -> tile mapping, summary, failures
  <hf>/LICENSES/                          copied from this repo

Zips are STORED (no recompression: .laz and deflate .tif are already compressed).
A domain/product is packaged only when every ROI that has data is present (use --force to
override). --delete-source removes the source files once their zip is written and verified,
so the disk never holds two copies of more than one zip.

Usage:
  python3 package_hf.py --dry-run
  python3 package_hf.py --delete-source
"""
import argparse
import os
import re
import shutil
import sys
import zipfile
from collections import defaultdict

from lidar_urls import FLAIR_DATA, OUTPUT, read_rois, human_bytes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(FLAIR_DATA)
DIR_RE = re.compile(r"^(D\d+-\d{4})_(?:LIDARHD_(LAZ|MNT|MNS|MNH)|BDORTHO_(IRC))$")


def failures(src):
    """{(ROI_id, product): reason} from failed.txt, keeping only the last reason per job."""
    out = {}
    path = os.path.join(src, "failed.txt")
    if os.path.exists(path):
        for line in open(path):
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 3:
                out[(parts[0], parts[1])] = parts[2]
    return out


def write_zip(entries, dest):
    part = dest + ".part"
    with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for arc, path in entries:
            z.write(path, arc)
    with zipfile.ZipFile(part) as z:      # verify: every member present with the right size
        sizes = {i.filename: i.file_size for i in z.infolist()}
    bad = [a for a, p in entries if sizes.get(a) != os.path.getsize(p)]
    if bad:
        raise RuntimeError(f"zip verification failed for {len(bad)} members, e.g. {bad[0]}")
    os.replace(part, dest)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=FLAIR_DATA, help="FLAIR-HUB data dir (contains GLOBAL_ALL_MTD.zip)")
    ap.add_argument("--lidar", default=os.path.join(ROOT, "data_lidar"))
    ap.add_argument("--vectors", default=os.path.join(ROOT, "data_vectors"))
    ap.add_argument("--hf", default=os.path.join(os.path.dirname(ROOT), "FLAIR-HUB-LIDARHD"))
    ap.add_argument("--domains", nargs="*", help="restrict to domains, e.g. D004 D024047")
    ap.add_argument("--products", nargs="*", help="restrict to products, e.g. MNT MNS IRC LAZ")
    ap.add_argument("--force", action="store_true", help="package incomplete domain/products too")
    ap.add_argument("--delete-source", action="store_true", help="delete source files after zipping")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rois = read_rois(args.data)
    fails = failures(args.lidar)
    expected = defaultdict(set)              # D004-2021 -> {ROI_id}
    for r in rois:
        expected[r.split("_", 1)[0]].add(r)

    data_dir = os.path.join(args.hf, "data")
    os.makedirs(data_dir, exist_ok=True)
    total = 0
    for d in sorted(os.listdir(args.lidar)):
        m = DIR_RE.match(d)
        if not m or (args.domains and m.group(1).split("-")[0] not in args.domains):
            continue
        dom_year, prod = m.group(1), m.group(2) or m.group(3)
        if args.products and prod not in [p.upper() for p in args.products]:
            continue
        dest = os.path.join(data_dir, f"{d}.zip")
        if os.path.exists(dest):
            continue
        src = os.path.join(args.lidar, d)
        files = {f: os.path.join(src, f) for f in os.listdir(src)
                 if not f.startswith(".") and ".part." not in f and not f.startswith("tmp")}
        present = {f"{dom_year}_{f.rsplit('_', 1)[1].rsplit('.', 1)[0]}" for f in files}
        missing = sorted(r for r in expected[dom_year] - present
                         if not any(k in fails.get((r, prod.lower()), "") for k in ("no LiDAR HD tile", "no coverage")))
        if missing and not args.force:
            print(f"skip {d}: {len(missing)} ROIs not downloaded yet (e.g. {missing[0]})", file=sys.stderr)
            continue
        size = sum(os.path.getsize(p) for p in files.values())
        total += size
        print(f"{d}.zip  {len(files)} files  {human_bytes(size)}")
        if args.dry_run:
            continue
        entries = [(f"{d}/{f[len(d) + 1:].rsplit('.', 1)[0]}/{f}", p) for f, p in sorted(files.items())]
        write_zip(entries, dest)
        if args.delete_source:
            shutil.rmtree(src)

    # Vectors: small, one zip per licence. OSM (ODbL, share-alike) is kept apart from the
    # IGN / Copernicus layers and carries its licence text inside the zip.
    gpkgs = sorted(f for f in os.listdir(args.vectors)
                   if f.startswith("FLAIR-HUB_") and f.endswith(".gpkg") and ".part." not in f)
    osm = [f for f in gpkgs if f.startswith("FLAIR-HUB_OSM_")]
    groups = {
        "GLOBAL_VECTORS": [(f, os.path.join(args.vectors, f)) for f in gpkgs if f not in osm],
        "GLOBAL_OSM": [(f, os.path.join(args.vectors, f)) for f in osm] + (
            [("osm_coverage.csv", os.path.join(args.vectors, "osm_coverage.csv")),
             ("LICENSE-ODbL-1.0.md", os.path.join(HERE, "LICENSES", "ODbL-1.0.md"))] if osm else []),
    }
    for name, members in groups.items():
        dest = os.path.join(data_dir, f"{name}.zip")
        # rebuilt whenever a member is newer than the zip (e.g. a layer finished merging later)
        stale = not os.path.exists(dest) or any(os.path.getmtime(p) > os.path.getmtime(dest) for _, p in members)
        if members and stale:
            print(f"{name}.zip  {', '.join(f for f, _ in members)}")
            if not args.dry_run:
                write_zip([(f"{name}/{f}", p) for f, p in members], dest)

    if not args.dry_run:
        meta = os.path.join(args.hf, "metadata")
        os.makedirs(meta, exist_ok=True)
        for f in ("flair_hub_lidar_tiles.csv", "flair_hub_lidar_roi_tiles.csv", "flair_hub_lidar_summary.json"):
            shutil.copy2(os.path.join(OUTPUT, f), meta)
        irc_log = os.path.join(args.lidar, "bdortho_irc_layers.csv")
        if os.path.exists(irc_log):
            shutil.copy2(irc_log, meta)
        for src, name in ((args.lidar, "failed_lidar.txt"), (args.vectors, "failed_vectors.txt")):
            if os.path.exists(os.path.join(src, name.replace("_lidar", ""))):
                shutil.copy2(os.path.join(src, name.replace("_lidar", "")), os.path.join(meta, name))
        shutil.copytree(os.path.join(HERE, "LICENSES"), os.path.join(args.hf, "LICENSES"), dirs_exist_ok=True)
    print(f"{'would write' if args.dry_run else 'wrote'} {human_bytes(total)} of raster / LAZ zips -> {data_dir}")


if __name__ == "__main__":
    main()
