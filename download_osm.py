#!/usr/bin/env python3
"""
Download OpenStreetMap land-cover features for each FLAIR-HUB ROI from the Overpass API,
clip them to the ROI and merge them into two GeoPackages with ROI_id / domain columns:

  FLAIR-HUB_OSM_LANDCOVER.gpkg  polygons: natural=*, landuse=*, wetland=*, leisure=park/garden/...
                                columns natural, landuse, leisure, wetland, leaf_type, leaf_cycle,
                                crop, produce, trees, surface, water (+ other_tags)
  FLAIR-HUB_OSM_LINES.gpkg      lines: barrier=hedge, natural=tree_row/cliff/ridge/arete/earth_bank
  osm_coverage.csv              per ROI: number of polygons, share of the ROI covered by OSM land cover

Overpass is a shared public service: one request at a time (each covering --batch ROIs, one bbox\nstatement per ROI), with back-off on 429 / 504.
Only features inside each ROI's bounding box are transferred (a few hundred MB for all ROIs).
Data: © OpenStreetMap contributors, ODbL 1.0 (see LICENSES/ODbL-1.0.md).

Outputs go to <out>/ (default: data_vectors next to FLAIR-HUB, like download_vectors.py);
per-ROI pieces are cached in <out>/_roi/osm_landcover and <out>/_roi/osm_lines (resumable).

Usage:
  python3 download_osm.py
  python3 download_osm.py --domains D004 --limit 3
"""
import argparse
import csv
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from download_vectors import merge
from lidar_urls import FLAIR_DATA, HEADERS, read_rois, urlopen_retry

HERE = os.path.dirname(os.path.abspath(__file__))
OVERPASS = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
OSMCONF = os.path.join(HERE, "config", "osmconf_landcover.ini")
LEISURE = "park|garden|golf_course|pitch|common|nature_reserve|dog_park"
LINES = "natural IN ('tree_row','cliff','ridge','arete','earth_bank') OR barrier = 'hedge'"
# one statement per ROI bbox, so a batch of ROIs costs one request without fetching what lies between
STATEMENTS = """  way[natural]({b}); relation[natural][type=multipolygon]({b});
  way[landuse]({b}); relation[landuse][type=multipolygon]({b});
  way[wetland]({b}); relation[wetland][type=multipolygon]({b});
  way[leisure~"^({leisure})$"]({b}); relation[leisure~"^({leisure})$"][type=multipolygon]({b});
  way[barrier=hedge]({b});"""
QUERY = """[out:xml][timeout:900][maxsize:1073741824];
(
{statements}
);
(._;>;);
out body qt;"""


def to_wgs84(rois):
    """{ROI_id: (s, w, n, e)} in degrees, from the Lambert-93 bbox corners (one gdaltransform call)."""
    ids = sorted(rois)
    pts = "".join(f"{x} {y}\n" for r in ids for x, y in
                  ((rois[r][0], rois[r][1]), (rois[r][0], rois[r][3]), (rois[r][2], rois[r][1]), (rois[r][2], rois[r][3])))
    out = subprocess.run(["gdaltransform", "-s_srs", "EPSG:2154", "-t_srs", "EPSG:4326", "-output_xy"],
                         input=pts, capture_output=True, text=True, check=True).stdout.split()
    lon, lat = list(map(float, out[0::2])), list(map(float, out[1::2]))
    pad = 1e-4                                  # ~10 m, the clip to the exact ROI is done in Lambert-93
    return {r: (min(lat[4 * i:4 * i + 4]) - pad, min(lon[4 * i:4 * i + 4]) - pad,
                max(lat[4 * i:4 * i + 4]) + pad, max(lon[4 * i:4 * i + 4]) + pad) for i, r in enumerate(ids)}


def overpass(bboxes, dest, retries=6):
    """Land-cover features inside any of the (s, w, n, e) boxes, as OSM XML in dest."""
    statements = "\n".join(STATEMENTS.format(b=",".join(f"{v:.6f}" for v in bb), leisure=LEISURE)
                            for bb in bboxes)
    data = urllib.parse.urlencode({"data": QUERY.format(statements=statements)}).encode()
    for attempt in range(retries):
        try:
            with urlopen_retry(urllib.request.Request(OVERPASS, data=data, headers=HEADERS)) as r:
                body = r.read()
            # Overpass reports timeouts / memory errors inside a 200 response
            if b"<remark>" in body[-2000:] and b"error" in body[-2000:]:
                raise RuntimeError(body[-500:].decode(errors="replace"))
            with open(dest, "wb") as fh:
                fh.write(body)
            return len(body)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(30 * 2 ** attempt)


def clip(osm, layer, where, bbox, dest):
    """OSM XML layer -> Lambert-93 GeoPackage clipped to bbox. Returns feature count."""
    part = dest + ".part.gpkg"
    if os.path.exists(part):
        os.remove(part)
    cmd = ["ogr2ogr", "-f", "GPKG", part, osm, layer, "-t_srs", "EPSG:2154",
           "-clipdst", *map(str, bbox), "-nlt", "PROMOTE_TO_MULTI", "-nln", dest.split(os.sep)[-2],
           "-skipfailures", "--config", "OSM_CONFIG_FILE", OSMCONF, "--config", "OSM_USE_CUSTOM_INDEXING", "NO"]
    if where:
        cmd += ["-where", where]
    subprocess.run(cmd, capture_output=True, text=True)
    n = feature_count(part, dest.split(os.sep)[-2]) if os.path.exists(part) else 0
    if n:
        os.replace(part, dest)
    else:
        if os.path.exists(part):
            os.remove(part)
        open(dest + ".empty", "w").close()
    return n


def feature_count(path, layer):
    info = subprocess.run(["ogrinfo", "-ro", "-so", path, layer], capture_output=True, text=True).stdout
    return int(next((l.split(":")[1] for l in info.splitlines() if l.startswith("Feature Count")), "0"))


def covered_share(path, layer, bbox):
    """Share of the ROI bbox covered by the union of the land-cover polygons (0-1)."""
    sql = f'SELECT ST_Area(ST_Union(geom)) AS a FROM "{layer}"'
    out = subprocess.run(["ogrinfo", "-ro", "-q", path, "-dialect", "SQLite", "-sql", sql],
                         capture_output=True, text=True).stdout
    area = next((float(l.split("=")[1]) for l in out.splitlines() if l.strip().startswith("a (")), 0.0)
    return min(1.0, area / ((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=FLAIR_DATA, help="FLAIR-HUB data dir (contains GLOBAL_ALL_MTD.zip)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(FLAIR_DATA), "data_vectors"))
    ap.add_argument("--domains", nargs="*", help="restrict to domains, e.g. D004 D024047")
    ap.add_argument("--limit", type=int, help="only the first N ROIs (testing)")
    ap.add_argument("--batch", type=int, default=25, help="ROIs per Overpass request")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between Overpass requests")
    args = ap.parse_args()

    rois = read_rois(args.data)
    if args.domains:
        rois = {k: v for k, v in rois.items() if k.split("-")[0] in args.domains}
    roi_ids = sorted(rois)[: args.limit] if args.limit else sorted(rois)
    wgs = to_wgs84({r: rois[r] for r in roi_ids})

    dirs = {name: os.path.join(args.out, "_roi", name) for name in ("osm_landcover", "osm_lines")}
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)
    done = lambda name, roi: any(os.path.exists(os.path.join(dirs[name], roi + ".gpkg" + ext)) for ext in ("", ".empty"))
    todo = [r for r in roi_ids if not (done("osm_landcover", r) and done("osm_lines", r))]
    print(f"{len(roi_ids)} ROIs, {len(todo)} to query on {OVERPASS}", file=sys.stderr)

    failed, transferred, t0 = [], 0, time.time()
    raw = os.path.join(args.out, "_roi", "_osm_raw.osm")
    batches = [todo[i:i + args.batch] for i in range(0, len(todo), args.batch)]   # sorted ids: same domain
    for i, batch in enumerate(batches, 1):
        try:
            transferred += overpass([wgs[r] for r in batch], raw)
        except Exception as e:
            failed += [f"{r}\tosm\t{e}" for r in batch]
            print(f"FAILED batch {batch[0]}..{batch[-1]}: {e}", file=sys.stderr)
            continue
        for roi in batch:
            try:
                clip(raw, "multipolygons", None, rois[roi], os.path.join(dirs["osm_landcover"], roi + ".gpkg"))
                clip(raw, "lines", LINES, rois[roi], os.path.join(dirs["osm_lines"], roi + ".gpkg"))
            except Exception as e:
                failed.append(f"{roi}\tosm\t{e}")
                print(f"FAILED {roi}: {e}", file=sys.stderr)
        print(f"  batch {i}/{len(batches)}  {transferred / 1e6:.0f} MB  {(time.time() - t0) / 60:.0f} min",
              file=sys.stderr)
        time.sleep(args.pause)
    if os.path.exists(raw):
        os.remove(raw)
    if failed:
        with open(os.path.join(args.out, "failed_vectors.txt"), "a") as fh:
            fh.write("\n".join(failed) + "\n")

    # merge + per-ROI coverage
    for name, final in (("osm_landcover", "FLAIR-HUB_OSM_LANDCOVER.gpkg"), ("osm_lines", "FLAIR-HUB_OSM_LINES.gpkg")):
        pieces = [(f[:-5], os.path.join(dirs[name], f)) for f in os.listdir(dirs[name])
                  if f.endswith(".gpkg") and not f.startswith(".") and ".part." not in f and f[:-5] in rois]
        if pieces:
            merge(name, pieces, os.path.join(args.out, final))
        print(f"{name}: {len(pieces)}/{len(roi_ids)} ROIs with features -> {final}")
    with open(os.path.join(args.out, "osm_coverage.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ROI_id", "n_polygons", "covered_share"])
        for roi in roi_ids:
            p = os.path.join(dirs["osm_landcover"], roi + ".gpkg")
            if os.path.exists(p):
                w.writerow([roi, feature_count(p, "osm_landcover"), f"{covered_share(p, 'osm_landcover', rois[roi]):.3f}"])
            else:
                w.writerow([roi, 0, "0.000"])
    print(f"{len(failed)} failed ROIs, {transferred / 1e6:.0f} MB transferred")


if __name__ == "__main__":
    main()
