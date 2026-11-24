#!/bin/bash
# Create the working data root data/work/. Stage 1 output (cleaned GPS pings, written to data/clean/pings by
# pipeline/01_extract.py) and the raw GPS archive are linked in; everything else is built here.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
W="$ROOT/data/work"
mkdir -p "$W/raw" "$W/clean/qc" "$W/model" "$W/live" "$ROOT/reports/results"
ln -sfn "$ROOT/data/raw/delhi-dtc-bus-gtfs-rt-2026-10" "$W/raw/gps"
ln -sfn "$ROOT/data/clean/pings" "$W/clean/pings"
[ -d "$W/raw/gtfs" ] || echo "Next: unzip the official static GTFS into $W/raw/gtfs/"
ls -l "$W/raw" "$W/clean"
