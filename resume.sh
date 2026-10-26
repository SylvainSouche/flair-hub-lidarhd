#!/bin/sh
# Resume / repair every download: report what is missing, verify existing files (broken ones are
# deleted and fetched again), then download whatever is missing. Safe to run any number of times.
#
#   ./resume.sh                 # rasters (MNT, MNS, IRC), then LAZ, then vectors
#   ./resume.sh --dry-run       # only print what is missing and the volume to download
#   PRODUCTS="irc" ./resume.sh  # a subset of the raster products
#
# Logs: $OUT/resume.log (OUT = data_lidar next to the FLAIR-HUB data folder).
set -u
cd "$(dirname "$0")"
DATA=${FLAIR_HUB_DATA:-/Volumes/SAMSUNG/sylvain/huggingface/FLAIR-HUB/data}
OUT=$(dirname "$DATA")/data_lidar
PRODUCTS=${PRODUCTS:-"mnt mns irc"}
LOG=$OUT/resume.log

if pgrep -f "download_lidar.py" >/dev/null; then
  echo "download_lidar.py is already running; wait for it to finish (or stop it) first." >&2
  exit 1
fi

echo "== missing before resume" | tee -a "$LOG"
python3 -u download_lidar.py --products $PRODUCTS laz --dry-run 2>>"$LOG" | tee -a "$LOG"
[ "${1:-}" = "--dry-run" ] && exit 0

echo "== rasters: $PRODUCTS (check existing, then fetch missing)" | tee -a "$LOG"
caffeinate -i python3 -u download_lidar.py --products $PRODUCTS --check-existing >>"$LOG" 2>&1
echo "== LAZ" | tee -a "$LOG"
caffeinate -i python3 -u download_lidar.py --products laz >>"$LOG" 2>&1
echo "== vectors" | tee -a "$LOG"
caffeinate -i python3 -u download_vectors.py >>"$LOG" 2>&1

echo "== still missing after resume" | tee -a "$LOG"
python3 -u download_lidar.py --products $PRODUCTS laz --dry-run 2>>"$LOG" | tee -a "$LOG"
grep -E "^(done|FAILED|BAD)" "$LOG" | tail -20
