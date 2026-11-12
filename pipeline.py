#!/usr/bin/env python3
"""
Batch pipeline that builds the Hugging Face dataset on GitHub Actions runners, so nothing large
goes through the local connection.

A *unit* is one zip of the dataset, i.e. one FLAIR-HUB domain and one product:
  D004-2021_LIDARHD_LAZ  D004-2021_LIDARHD_MNT  D004-2021_LIDARHD_MNS  D004-2021_LIDARHD_MNH
  D004-2021_BDORTHO_IRC
For each unit the runner downloads and crops every ROI of the domain (same code as
download_lidar.py), appends each cropped file to data/<unit>.zip and deletes it right away, uploads
the zip to the Hub, deletes the zip, and moves the unit from todo to done. When the time budget of
the run is spent, the workflow starts the next run (.github/workflows/pipeline.yml).

State directory (kept on the `pipeline-state` branch by the workflow, committed after every unit):
  todo.txt    one unit per line, processed in order
  done.txt    unit, files, bytes, minutes, finished at (UTC), note
  failed.txt  unit, attempts, last error (a unit is dropped from todo after MAX_ATTEMPTS)
  stalls.txt  consecutive runs that finished no unit (the chain stops at MAX_STALLS)

Usage:
  python3 pipeline.py export-inputs                 # local, once: pipeline/rois.csv, pipeline/laz_tiles.csv
  python3 pipeline.py init   --state state          # todo = every unit not already on the Hub
  python3 pipeline.py run    --state state --work /mnt/work --budget-min 300 --git-push
  python3 pipeline.py status --state state
  python3 pipeline.py run    --state /tmp/s --work /tmp/w --units D010-2019_LIDARHD_MNH --no-upload  # local test
"""
import argparse
import csv
import datetime
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from download_lidar import RASTERS, TileCache, fetch_laz, fetch_raster, raster_shape
from lidar_urls import FLAIR_DATA, OUTPUT, read_rois, tiles_for_bbox

HERE = os.path.dirname(os.path.abspath(__file__))
INPUTS = os.path.join(HERE, "pipeline")
REPO_ID = os.environ.get("HF_REPO", "SylvainSouche/FLAIR-HUB-LiDAR-HD")
TAGS = {"laz": "LIDARHD_LAZ", "mnt": "LIDARHD_MNT", "mns": "LIDARHD_MNS", "mnh": "LIDARHD_MNH",
        "irc": "BDORTHO_IRC"}
PRODUCT_OF = {v: k for k, v in TAGS.items()}
MAX_ATTEMPTS = 3
MAX_STALLS = 3
# Conservative throughput used to decide whether a unit still fits in the remaining budget
LAZ_MBPS = 15           # MB/s of COPC tiles from data.geopf.fr/telechargement (1 request/s cap)
WMS_MPX_PER_MIN = 150   # megapixels per minute from the WMS-R, all workers together


# ---------------------------------------------------------------- inputs

def export_inputs(data):
    """Write the small inputs the runner needs (no FLAIR-HUB download on the runner)."""
    os.makedirs(INPUTS, exist_ok=True)
    rois = read_rois(data)
    with open(os.path.join(INPUTS, "rois.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ROI_id", "xmin", "ymin", "xmax", "ymax"])
        for r in sorted(rois):
            w.writerow([r, *(f"{v:.3f}".rstrip("0").rstrip(".") for v in rois[r])])
    with open(os.path.join(OUTPUT, "flair_hub_lidar_tiles.csv")) as src, \
            open(os.path.join(INPUTS, "laz_tiles.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["tile", "url_laz", "laz_bytes"])
        for r in csv.DictReader(src):
            if r["url_laz"]:
                w.writerow([r["tile"], r["url_laz"], r["laz_bytes"]])
    print(f"{len(rois)} ROIs -> {INPUTS}/rois.csv, LAZ tiles -> {INPUTS}/laz_tiles.csv")


def load_inputs():
    rois = read_rois(os.path.join(INPUTS, "rois.csv"))
    urls, sizes = {}, {}
    with open(os.path.join(INPUTS, "laz_tiles.csv")) as fh:
        for r in csv.DictReader(fh):
            urls[r["tile"]] = r["url_laz"]
            sizes[r["tile"]] = int(r["laz_bytes"]) if r["laz_bytes"] else 150_000_000
    return rois, urls, sizes


def all_units(rois, products):
    dom_years = sorted({r.split("_", 1)[0] for r in rois})
    return [f"{d}_{TAGS[p]}" for d in dom_years for p in products]


def split_unit(unit):
    dom_year, tag = unit.split("_", 1)
    return dom_year, PRODUCT_OF[tag]


# ---------------------------------------------------------------- state

class State:
    def __init__(self, path, git_push=False):
        self.path, self.git_push = path, git_push
        os.makedirs(path, exist_ok=True)

    def _file(self, name):
        return os.path.join(self.path, name)

    def read(self, name):
        if not os.path.exists(self._file(name)):
            return []
        with open(self._file(name)) as fh:
            return [line.rstrip("\n").split("\t") for line in fh if line.strip()]

    def write(self, name, rows):
        with open(self._file(name), "w") as fh:
            fh.writelines("\t".join(map(str, r)) + "\n" for r in rows)

    def todo(self):
        return [r[0] for r in self.read("todo.txt")]

    def commit(self, message):
        if not self.git_push:
            return
        git = ["git", "-C", self.path]
        subprocess.run(git + ["add", "-A"], check=True)
        if subprocess.run(git + ["diff", "--cached", "--quiet"]).returncode == 0:
            return
        subprocess.run(git + ["commit", "-q", "-m", message], check=True)
        for _ in range(3):
            if subprocess.run(git + ["push", "-q", "origin", "HEAD:pipeline-state"]).returncode == 0:
                return
            time.sleep(10)
        print("WARNING: could not push the state branch", file=sys.stderr)

    def mark_done(self, unit, files, size, minutes, note=""):
        self.write("todo.txt", [[u] for u in self.todo() if u != unit])
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
        self.write("done.txt", self.read("done.txt") + [[unit, files, size, f"{minutes:.1f}", stamp, note]])
        self.write("failed.txt", [r for r in self.read("failed.txt") if r[0] != unit])
        self.commit(f"done {unit} ({files} files, {size / 1e9:.2f} GB, {minutes:.0f} min)")

    def mark_failed(self, unit, error):
        failed = {r[0]: r for r in self.read("failed.txt")}
        attempts = int(failed.get(unit, [unit, 0])[1]) + 1
        failed[unit] = [unit, attempts, " ".join(str(error).split())[:300]]
        self.write("failed.txt", list(failed.values()))
        todo = [u for u in self.todo() if u != unit]
        if attempts < MAX_ATTEMPTS:
            todo.append(unit)               # retry later, after the other units
        self.write("todo.txt", [[u] for u in todo])
        self.commit(f"failed {unit} (attempt {attempts}/{MAX_ATTEMPTS})")


def hub_zips(api):
    files = api.list_repo_files(REPO_ID, repo_type="dataset")
    return {f[len("data/"):-len(".zip")] for f in files if f.startswith("data/") and f.endswith(".zip")}


# ---------------------------------------------------------------- one unit

def estimate_minutes(unit, rois, laz_sizes):
    dom_year, prod = split_unit(unit)
    ids = [r for r in rois if r.startswith(dom_year + "_")]
    if prod == "laz":
        tiles = {t for r in ids for t in tiles_for_bbox(*rois[r]) if t in laz_sizes}
        return sum(laz_sizes[t] for t in tiles) / (LAZ_MBPS * 1e6) / 60 + 0.3 * len(ids)
    mpx = sum(w * h for w, h in (raster_shape(prod, rois[r]) for r in ids)) / 1e6
    return mpx / WMS_MPX_PER_MIN + 0.1 * len(ids)


def build_unit(unit, rois, laz_urls, work, workers):
    """Download, crop and zip every ROI of the unit. Returns (zip path or None, files, extra files)."""
    dom_year, prod = split_unit(unit)
    ids = sorted(r for r in rois if r.startswith(dom_year + "_"))
    tmp = os.path.join(work, unit)
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    zpath = os.path.join(work, f"{unit}.zip")
    ext = "laz" if prod == "laz" else "tif"
    jobs = {r: os.path.join(tmp, f"{unit}_{r.split('_', 1)[1]}.{ext}") for r in ids}

    cache = None
    if prod == "laz":
        need = {r: sorted(t for t in tiles_for_bbox(*rois[r]) if t in laz_urls) for r in ids}
        jobs = {r: p for r, p in jobs.items() if need[r]}         # ROIs without LiDAR HD coverage
        refs = defaultdict(int)
        for r in jobs:
            for t in need[r]:
                refs[t] += 1
        cache = TileCache(os.path.join(work, "_tiles_cache"), laz_urls, refs)
    if not jobs:
        shutil.rmtree(tmp, ignore_errors=True)
        return None, 0, []

    def one(roi):
        if prod == "laz":
            fetch_laz(rois[roi], need[roi], cache, jobs[roi])
        else:
            fetch_raster(prod, roi, rois[roi], jobs[roi], tmp)
        return roi

    lock, added, errors = threading.Lock(), [], []
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED, allowZip64=True) as z, \
            ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(one, r) for r in jobs]
        for i, f in enumerate(as_completed(futs), 1):
            try:
                roi = f.result()
            except Exception as e:
                errors.append(str(e))
                print(f"  FAILED ROI in {unit}: {e}", file=sys.stderr)
                continue
            path = jobs[roi]
            name = os.path.basename(path)
            with lock:          # same layout as FLAIR-HUB: <unit>/<roi>/<unit>_<roi>.<ext>
                z.write(path, f"{unit}/{name[len(unit) + 1:].rsplit('.', 1)[0]}/{name}")
                added.append(name)
            os.remove(path)
            if i % 20 == 0:
                print(f"  {unit}: {i}/{len(jobs)} ROIs", file=sys.stderr)
    if errors:
        raise RuntimeError(f"{len(errors)}/{len(jobs)} ROIs failed, e.g. {errors[0][:200]}")
    with zipfile.ZipFile(zpath) as z:
        if len(z.namelist()) != len(jobs) or z.testzip() is not None:
            raise RuntimeError("zip verification failed")
    extras = [os.path.join(tmp, f) for f in os.listdir(tmp) if f.endswith("_layers.csv")]
    return zpath, len(added), extras


def upload(api, unit, zpath, extras):
    api.upload_file(path_or_fileobj=zpath, path_in_repo=f"data/{unit}.zip", repo_id=REPO_ID,
                    repo_type="dataset", commit_message=f"Add {unit}")
    for path in extras:         # e.g. the BD ORTHO edition used per ROI
        api.upload_file(path_or_fileobj=path, repo_id=REPO_ID, repo_type="dataset",
                        path_in_repo=f"metadata/{os.path.basename(path)[:-4]}/{unit}.csv",
                        commit_message=f"Metadata for {unit}")


# ---------------------------------------------------------------- commands

def cmd_init(args):
    from huggingface_hub import HfApi
    rois, _, _ = load_inputs()
    state = State(args.state, args.git_push)
    units = all_units(rois, args.products)
    on_hub = hub_zips(HfApi())
    done_units = {r[0] for r in state.read("done.txt")}
    todo = [u for u in units if u not in on_hub and u not in done_units]
    done = state.read("done.txt") + [[u, "", "", "", "", "already on the Hub"]
                                     for u in units if u in on_hub and u not in done_units]
    state.write("todo.txt", [[u] for u in todo])
    state.write("done.txt", done)
    state.write("stalls.txt", [["0"]])
    state.commit(f"init: {len(todo)} units to do, {len(done)} done")
    print(f"{len(units)} units: {len(todo)} to do, {len(units) - len(todo)} already on the Hub")


def cmd_run(args):
    rois, laz_urls, laz_sizes = load_inputs()
    state = State(args.state, args.git_push)
    api = None
    if not args.no_upload:
        from huggingface_hub import HfApi
        api = HfApi()
        on_hub = hub_zips(api)
    os.makedirs(args.work, exist_ok=True)
    t0, finished = time.time(), 0
    todo = args.units or state.todo()
    for unit in todo:
        elapsed = (time.time() - t0) / 60
        est = estimate_minutes(unit, rois, laz_sizes)
        if finished and elapsed + 1.5 * est > args.budget_min:
            print(f"budget: {elapsed:.0f} min used, {unit} needs ~{est:.0f} min -> next run", file=sys.stderr)
            break
        if args.max_units and finished >= args.max_units:
            break
        if api and unit in on_hub:          # uploaded by a run whose state commit was lost
            state.mark_done(unit, "", "", 0, "found on the Hub")
            continue
        print(f"== {unit} (estimate {est:.0f} min)", file=sys.stderr)
        t1 = time.time()
        try:
            zpath, files, extras = build_unit(unit, rois, laz_urls, args.work, args.workers)
            size = os.path.getsize(zpath) if zpath else 0
            if zpath and api:
                upload(api, unit, zpath, extras)
            if zpath and not args.keep:
                os.remove(zpath)
            shutil.rmtree(os.path.join(args.work, unit), ignore_errors=True)
            minutes = (time.time() - t1) / 60
            note = "" if zpath else "no data (no LiDAR HD coverage)"
            if args.units:
                print(f"   {unit}: {files} files, {size / 1e9:.2f} GB, {minutes:.1f} min {note}", file=sys.stderr)
            else:
                state.mark_done(unit, files, size, minutes, note)
            finished += 1
        except Exception as e:
            print(f"   {unit} FAILED: {e}", file=sys.stderr)
            if not args.units:
                state.mark_failed(unit, e)
            shutil.rmtree(os.path.join(args.work, unit), ignore_errors=True)
            if os.path.exists(os.path.join(args.work, f"{unit}.zip")):
                os.remove(os.path.join(args.work, f"{unit}.zip"))
    shutil.rmtree(os.path.join(args.work, "_tiles_cache"), ignore_errors=True)

    if args.units:
        return
    stalls = 0 if finished else int((state.read("stalls.txt") or [["0"]])[0][0]) + 1
    state.write("stalls.txt", [[stalls]])
    remaining = len(state.todo())
    state.commit(f"run: {finished} units finished, {remaining} to do")
    go_on = remaining > 0 and stalls < MAX_STALLS
    print(f"finished {finished} units in {(time.time() - t0) / 60:.0f} min, {remaining} to do, "
          f"{'continue' if go_on else 'stop'}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"remaining={remaining}\ncontinue={'true' if go_on else 'false'}\n")


def cmd_status(args):
    state = State(args.state)
    done, todo, failed = state.read("done.txt"), state.todo(), state.read("failed.txt")
    size = sum(int(r[2]) for r in done if len(r) > 2 and r[2].isdigit())
    print(f"done {len(done)}  todo {len(todo)}  failed {len(failed)}  uploaded {size / 1e9:.1f} GB")
    by = defaultdict(int)
    for u in todo:
        by[split_unit(u)[1]] += 1
    print("todo by product:", dict(by))
    for r in failed:
        print("failed:", *r)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("export-inputs")
    p.add_argument("--data", default=FLAIR_DATA)
    for name in ("init", "run", "status"):
        p = sub.add_parser(name)
        p.add_argument("--state", default="state")
        p.add_argument("--git-push", action="store_true", help="commit + push the state dir after each change")
        if name == "init":
            p.add_argument("--products", nargs="+", default=["laz", "mnt", "mns", "mnh", "irc"],
                           choices=list(TAGS))
        if name == "run":
            p.add_argument("--work", default="/mnt/work")
            p.add_argument("--budget-min", type=float, default=300)
            p.add_argument("--max-units", type=int, default=0)
            p.add_argument("--workers", type=int, default=3)
            p.add_argument("--units", nargs="*", help="process these units only, without touching the state")
            p.add_argument("--no-upload", action="store_true")
            p.add_argument("--keep", action="store_true", help="keep the zip in --work")
    args = ap.parse_args()
    {"export-inputs": lambda a: export_inputs(a.data), "init": cmd_init, "run": cmd_run,
     "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
