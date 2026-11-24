"""All paths and parameters in one place (tuned on training days only).

Paths can be redirected with environment variables, which the synthetic end-to-end test uses:
  DTC_DATA     data root (default <repo>/data)
  DTC_GTFS     static GTFS folder (default $DTC_DATA/raw/gtfs)
  DTC_REPORTS  report output folder (default <repo>/reports)
  DTC_TRAIN_DAYS / DTC_TEST_DAYS  comma-separated YYYY-MM-DD lists
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Working data root: official GTFS, stop events, model, live feed (created by tools/setup_data.sh)
DATA = Path(os.environ.get("DTC_DATA", ROOT / "data/work"))
GTFS_DIR = Path(os.environ.get("DTC_GTFS", DATA / "raw/gtfs"))
PINGS = DATA / "clean/pings"
STOP_EVENTS = DATA / "clean/stop_events"
QC = DATA / "clean/qc"
MODEL = Path(os.environ.get("DTC_MODEL", DATA / "model"))
REPORTS = Path(os.environ.get("DTC_REPORTS", ROOT / "reports/results"))
LIVE_DIR = Path(os.environ.get("DTC_LIVE", DATA / "live"))     # live worker output (tools/rt_live.py)

IST_OFFSET_S = 5 * 3600 + 30 * 60


def _days(env: str, default: list[str]) -> list[str]:
    v = os.environ.get(env)
    return [d.strip() for d in v.split(",") if d.strip()] if v else default


# Train on the last two weeks before the test days, without Dussehra (20 Oct). Chosen in the improvement run
# (reports/IMPROVEMENTS.md): two weeks beat one week on both tuning splits; older data (1-13 Oct) made it worse.
TRAIN_DAYS = _days("DTC_TRAIN_DAYS", [f"2026-10-{d}" for d in (14, 15, 16, 17, 18, 19, 21, 22, 23, 24, 25, 26, 27)])
TEST_DAYS = _days("DTC_TEST_DAYS", ["2026-10-28", "2026-10-31"])
HOLIDAYS: set[str] = set()          # service dates to treat as weekend (none in the October data)

# ---- Stage 2-3: trip matching and stop events ---------------------------------------
SNAP_RADIUS_M = 50.0                # a stop counts as passed if the bus track comes this close
RUN_GAP_S = 15 * 60                 # split a vehicle's stream after a gap longer than this
MIN_PINGS_PER_RUN = 10
START_WINDOW_S = 30 * 60            # GTFS trip start must be within this of the trip_id's HH:MM ...
START_WINDOW_WIDE_S = 90 * 60       # ... or, if nothing is found, within this
# Runs matching 30-60% of their pattern are mostly partial runs that still carry many real stop passings.
# Segment times only need consecutive observed stops, and validation uses only runs with >= FULL_TRIP_FRAC.
MIN_MATCHED_STOPS = 8
MIN_MATCHED_FRAC = 0.3              # share of the pattern's stops matched, to keep a run
FULL_TRIP_FRAC = 0.9                # trips used for end-to-end validation
SEG_MIN_KMH, SEG_MAX_KMH = 1.0, 80.0

# ---- Stage 4: segment model -------------------------------------------------------------
SHRINK_K = 8.0                      # shrinkage strength towards the parent estimate
EST_QUANTILE = 0.5                  # quantile of observed segment times used as the estimate (0.5 = median)
RECENT_DAYS = 0                     # count the last N training days twice in the model (0: off)
EST_MEAN_W = 0.0                    # estimate = (1 - w) * quantile + w * trimmed mean (sums of skewed segment times)
TREND_DAYS = 0                      # >0: scale estimates by how much slower the last N training days ran
N_DAYTYPES = 2                      # 2: weekday / weekend;  3: weekday / Saturday / Sunday+holiday
VALIDATION_SAMPLES = 1500
FALLBACK_ROUTE = 0                  # 1: unobserved segments use city speed x this route's observed/city ratio
FALLBACK_K = 5.0                    # shrinkage of that ratio towards 1
PLAN_BUFFER = 1.0                   # "plan for" = estimate + b x (summed segment P85 - estimate); 0.6 hit 85% on tuning days but not on the slower test days
HOUR_INTERP = 1                     # 1: interpolate hourly segment profiles between hour centres
LIVE_IN_API = 1                     # API: for dates with GPS stop events, also return a live-blend time (replay)
LIVE_REPLAY = 1                     # 1: also score the live blend by replaying the test day (transit/live.py)
LIVE_WINDOW_S = 1200                # live data used: segment traversals that ended in the last 20 min (tuned)
LIVE_M = 1.0                        # per-segment live weight  n / (n + M) * exp(-age / TAU)
LIVE_TAU_S = 1800.0
LIVE_CITY_WINDOW_S = 0             # city-wide factor from all buses in this window before boarding (0: off)
LIVE_K_CITY = 50.0                  # city factor shrinkage towards 1
LIVE_OWN_WINDOW_S = 1800              # own-bus factor: this bus's segments before boarding, in this window (0: off)
LIVE_OWN_CLIP = 1.3                 # cap the own-bus ratio to [1/c, c] (0: no cap)
LIVE_K_OWN = 20.0                    # own-bus factor shrinkage towards the corridor factor
LIVE_CORR_WINDOW_S = 3600              # corridor factor window (0: same as LIVE_WINDOW_S)
LIVE_K_CORRIDOR = 5.0               # corridor factor shrinkage  1 + (r - 1) * n / (n + K)
DEFAULT_KMH = 15.0                  # used only if a day type / hour has no data at all

# ---- Planner -------------------------------------------------------------------------------
WALK_KMH = 4.5
WALK_DETOUR = 1.3
TRANSFER_RADIUS_M = 400.0
ACCESS_RADIUS_M = 800.0
ACCESS_RADIUS_WIDE_M = 1500.0
MIN_ACCESS_STOPS = 3
MAX_ROUNDS = 3                      # at most 3 buses (2 transfers)
TRANSFER_BUFFER_S = 60
MAX_OPTIONS = 3
OFFPEAK_DAYTYPE, OFFPEAK_HOUR = 0, 23   # "quiet hour" reference: the faster of weekday 23:00 and 02:00


def daytype_of(date_str: str) -> int:
    """0 = weekday, 1 = weekend (Saturday, Sunday or a listed holiday); with N_DAYTYPES = 3: 1 = Saturday, 2 = Sunday/holiday."""
    import datetime as dt
    d = dt.date.fromisoformat(date_str)
    if N_DAYTYPES == 3:
        return 2 if d.isoweekday() == 7 or date_str in HOLIDAYS else (1 if d.isoweekday() == 6 else 0)
    return 1 if d.isoweekday() >= 6 or date_str in HOLIDAYS else 0


# Parameter overrides for tuning runs, e.g. DTC_SET="EST_QUANTILE=0.6,SHRINK_K=4"
for _kv in filter(None, os.environ.get("DTC_SET", "").split(",")):
    _k, _v = _kv.split("=", 1)
    globals()[_k.strip()] = type(globals()[_k.strip()])(_v)
