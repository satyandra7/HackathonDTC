# HackathonDTC

**Transport Stack Hackathon 2026 (Delhi), Problem 1:** a real-time bus traffic model and journey planner.

**Team THE HEURISTIC LARPERS:** Satyandra Pal (team lead), Anoushka Malik.

Delhi's buses report their GPS position every few seconds. This project uses that fleet as a set of moving
traffic sensors: it cleans a month of historical positions, matches each bus run to its scheduled trip in the
official DTC GTFS, turns the GPS track into stop-passing times, and learns how long every stop-to-stop segment
takes by day type and hour. A live worker processes the authority's GTFS-RT feed the same way every 2 minutes, and
a journey planner (time-dependent RAPTOR) combines the historical and live segment times to return bus journey
options between two points.

| Deliverable | Where |
|---|---|
| 1. Proposal | [`reports/proposal.pdf`](reports/proposal.pdf) |
| 2. Working prototype | journey planner website + API + live worker: see [Running the planner](#running-the-planner) |
| 3. Source code and setup | this repository and README |
| 4. Technical note | [`reports/technical_note.pdf`](reports/technical_note.pdf) (validation: [`reports/validation_report.pdf`](reports/validation_report.pdf)) |
| 5. Demo video | [[TBD: link]] |

- **Proposal:** [`reports/proposal.pdf`](reports/proposal.pdf) · **Technical note:**
  [`reports/technical_note.pdf`](reports/technical_note.pdf) · **Validation report:**
  [`reports/validation_report.pdf`](reports/validation_report.pdf)
- Result data (per-ride predictions, metrics): [`reports/results/`](reports/results/)

## Results

All data come from the authority (delhi.transportstack.in): the October 2026 GPS archive, the official static GTFS
and the live GTFS-RT feed.

| | Our model + live data | Our model, history only | Official timetable | Google Maps |
|---|---|---|---|---|
| Held-out test days, 1,500 rides | **4.48 min (13.8%)** | 5.82 min (16.5%) | 8.98 min (30.3%) | — |
| 300 of those rides, with Google Maps | **4.89 min (14.1%)** | 6.44 min (17.5%) | 9.65 min (30.6%) | 10.16 min (33.6%) |
| Live feed, 24 Nov evening, 1,500 rides | **2.00 min (19.5%)** | 2.99 min (28.8%) | 3.44 min (28.6%) | — |

Mean absolute error (MAPE). 2,191 of 2,203 GPS routes in the official GTFS, 12,847 road segments (85%) learned from
15.6 M observations, about 5,500 live buses. A journey query takes 0.2–0.6 s.

## Pipeline

| Stage | What it does | Script |
|---|---|---|
| 1 | Extract and clean GPS pings (archive) | `pipeline/01_extract.py` |
| 2–3 | Match runs to GTFS trips, compute stop passing times | `pipeline/02_stop_events.py` |
| 4 | Segment travel-time model (segment × day type × hour) | `pipeline/04_model.py` |
| 5 | Road shapes for the map, cut from `shapes.txt` | `pipeline/05_shapes.py` |
| Live | Poll the GTFS-RT feed; clean, match and rebuild live stop events every 2 min | `tools/rt_live.py`, `transit/rt.py` |
| Blend | Combine live and historical segment times | `transit/live.py` |
| 6–7 | RAPTOR planner, FastAPI, Leaflet map | `transit/planner.py`, `api/main.py`, `web/index.html` |
| 8 | Validation: held-out days, Google Maps, live feed | `pipeline/08_validate.py`, `tools/gmaps_lookup.py`, `tools/live_check.py` |

## Repository layout

```
transit/       shared Python package
  config.py      every path and parameter in one place (overridable via env vars, see below)
  gtfs.py        static GTFS loader: patterns, active services per date, times past 24:00
  matching.py    GPS track to stop-sequence matching
  live.py        live + historical blend
  rt.py          GTFS-RT fetch, decode and cleaning
  planner.py     network build and time-dependent RAPTOR with model-based ride times
pipeline/      numbered pipeline stages, run in order
api/main.py    FastAPI service (/journeys, /stops/search, /health, /docs) serving the map page
web/index.html Leaflet map client
tools/         live worker, live-feed checks, Google Maps lookup, data setup
tests/         synthetic GTFS + GPS fixture for an end-to-end test
reports/       proposal, technical note, validation report (.md + .pdf); results/ = their data
data/          git-ignored: raw GPS archive, stage 1 output, data/work/ (GTFS, model, live feed)
```

## Setup

Python 3.11+.

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt gtfs-realtime-bindings
```

### Getting the data (all from delhi.transportstack.in)

1. **GPS archive** (16 GB):
   <https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/delhi-dtc-bus-gtfs-rt-2026-10.7z>, extracted into
   `data/raw/` (it unpacks to `delhi-dtc-bus-gtfs-rt-2026-10/`).
2. **Official static GTFS** (dataset 1, `delhi_buses_static_gtfs_v1`): unzip into `data/work/raw/gtfs/` (after `bash tools/setup_data.sh` below).
3. **Live GTFS-RT feed** (dataset 7, free, approved automatically): subscribe, and save the API key from the
   approval email (it is sent in an `x-api-key` header; never put it in the repo):

   ```bash
   install -m 600 /dev/null ~/.otd_key && echo 'YOUR_KEY' > ~/.otd_key
   ```

## Running the pipeline

**Stage 1: extract and clean** the archive (reads the SQLite files read-only; geofence, dedup, clock sanity,
teleport filter, stationary compression; Parquet plus a QC report per day):

```bash
.venv/bin/python pipeline/01_extract.py --all --threads 16
```

Then create the working data root `data/work/` (put the official GTFS in `data/work/raw/gtfs/`):

```bash
bash tools/setup_data.sh
```

**Stages 2–5:** trip matching and stop events on the train and test days, the segment model, road shapes:

```bash
.venv/bin/python pipeline/02_stop_events.py --train-test --workers 4 && .venv/bin/python pipeline/04_model.py && .venv/bin/python pipeline/05_shapes.py
```

**Stage 8: validation** on the held-out days (writes `reports/results/validation.md` and samples), then the Google
Maps comparison (needs a Google Maps Platform key with the Routes API, in `~/.gmaps_key`):

```bash
.venv/bin/python pipeline/08_validate.py
```

```bash
.venv/bin/python tools/gmaps_lookup.py --n 300
```

**Live feed:** check the feed, run the live worker (one low-priority process; polls every 15 s, rebuilds every
2 min), and after about an hour check live predictions against what the buses then did:

```bash
.venv/bin/python tools/rt_check.py
```

```bash
nice -n 5 .venv/bin/python tools/rt_live.py
```

```bash
.venv/bin/python tools/live_check.py --warmup 25
```

## Running the planner

With the live worker running (for live answers):

```bash
.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8778
```

Open <http://localhost:8778/> for the map, or <http://localhost:8778/docs> for the API docs. Example:

```bash
curl -G localhost:8778/journeys --data-urlencode "from=Kashmere Gate ISBT" --data-urlencode "to=Nehru Place"
```

Each option lists walk / bus legs (boarding and alighting stops, departure and arrival, wait, ride time, the
timetable's ride time), the total, a P85 "plan for" time, the same trip at a quiet hour (the faster of weekday
23:00 and 02:00) and the resulting traffic delay, a confidence, and, when live data is available for that date,
the live-adjusted total. `depart` defaults to now.

## End-to-end test on synthetic data

```bash
F=/tmp/dtc-fixture && .venv/bin/python tests/make_fixture.py $F
```

```bash
export DTC_DATA=/tmp/dtc-fixture DTC_REPORTS=/tmp/dtc-fixture/reports DTC_MODEL=/tmp/dtc-fixture/model DTC_TRAIN_DAYS=2026-10-21,2026-10-22,2026-10-23,2026-10-24,2026-10-25,2026-10-26,2026-10-27
```

```bash
.venv/bin/python pipeline/02_stop_events.py --train-test && .venv/bin/python pipeline/04_model.py && .venv/bin/python pipeline/08_validate.py
```

## Configuration

All parameters (snap radius, run gap, shrinkage strength, walking speed, transfer limits, …) live in
[`transit/config.py`](transit/config.py) and were tuned on training days only. Paths and day sets can be
redirected with environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `DTC_DATA` | `<repo>/data/work` | data root (official GTFS, stop events, model, live feed) |
| `DTC_GTFS` | `$DTC_DATA/raw/gtfs` | static GTFS folder |
| `DTC_MODEL` | `$DTC_DATA/model` | model folder |
| `DTC_REPORTS` | `<repo>/reports/results` | validation and check outputs |
| `DTC_LIVE` | `$DTC_DATA/live` | live worker output |
| `DTC_TRAIN_DAYS` | `2026-10-14` … `2026-10-27` (13 days, without 20 Oct) | comma-separated YYYY-MM-DD |
| `DTC_TEST_DAYS` | `2026-10-28,2026-10-31` | comma-separated YYYY-MM-DD |
