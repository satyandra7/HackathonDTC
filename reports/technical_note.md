# Technical note: bus travel times from the DTC fleet

**Team THE HEURISTIC LARPERS** (Satyandra Pal, Anoushka Malik) · Transport Stack Hackathon 2026 · Problem 1: real-time bus traffic model and
journey planner · Code: <https://github.com/satyandra7/HackathonDTC>

> 24 Nov 2026. All data come from the authority: the October 2026 bus GPS archive, the official DTC static GTFS
> (`delhi_buses_static_gtfs_v1`) and the live GTFS-RT feed, all from delhi.transportstack.in.

## 1. In one paragraph

We treat every DTC bus as a moving speed sensor. A month of GPS positions (1–31 October 2026, 913 million rows)
is cleaned, each bus run is matched to its scheduled trip in the official GTFS, and the GPS track is turned into
the time the bus passed each stop. From the times between consecutive stops we learn how long every stop-to-stop
**segment** takes, by **day type** (weekday or weekend) and **hour**. The live GTFS-RT feed goes through the same
cleaning and matching every 2 minutes, and its fresh segment times adjust the historical ones. A journey planner
(time-dependent RAPTOR) uses the GTFS for *when buses leave* and our model for *how long they take*, so a trip
at 9 am on a weekday gets a different, traffic-aware answer from the same trip at 11 pm, and tonight's answer
reflects tonight's traffic.

## 2. Data

| Source (delhi.transportstack.in) | Use |
|---|---|
| Historical bus GPS, 1–31 Oct 2026: 31 SQLite files, 124 GB | Learning segment times |
| Official static GTFS (dataset 1): 6,862 stops, 2,574 routes, 80,357 trips, 3,498 stop patterns, `shapes.txt`; calendar 2025–2040 | Stops, route stop sequences, departure times, road shapes |
| Live GTFS-RT VehiclePositions (dataset 7): about 5,500 buses, `x-api-key` header | Live adjustment of travel times |

- **Train:** 14–19 and 21–27 Oct (13 days; Dussehra on 20 Oct excluded). Two weeks beat one week and four weeks
  on training-day tuning splits.
- **Test (held out, scored once):** Wed 28 and Sat 31 Oct. **Live check:** the real feed on 24 Nov, 18:29–19:30.

**What the live feed contains** (checked on 5,492 vehicles): vehicle ID, `trip_id`, `route_id`, trip start
time and date, position and timestamp are always filled; `stop_id`, `current_stop_sequence`, `direction_id` and
bearing are not. Positions are 41 s old on median. So live buses must be matched to stops by us, exactly like the
archive.

## 3. Cleaning the GPS data (Stage 1)

Every database is opened read-only. Rules, in order (rows from all 31 days):

| Rule | Removed | Why |
|---|---|---|
| Exact duplicates on (vehicle, GPS time) | 38.6% | The feed was polled faster than buses report |
| Outside a Delhi bounding box | 0.14% | GPS errors (points as far as latitude 17) |
| Fix saved before its GPS time, or > 30 min after | 0.02% | Clock errors, stale replays |
| "Teleports": > 90 km/h, or > 1 km jump in ≤ 10 s (two passes) | 0.09% | GPS jumps |
| Identical consecutive positions merged into one dwell record | 1.9% | Keeps when the bus arrived and left without the repeated rows |

**Result:** 549 M clean records (60%), median reporting interval 11 s. The live worker applies the same rules to
its rolling window.

**Two data findings shaped the design** (both confirmed by the organisers):
- **The `speed` field is not reliable**, so speed is always calculated from positions and times.
- **The feed's `trip_id` is not a GTFS trip ID.** It is `ROUTE_HH_MM[_NNNN]`; its prefix is the `route_id`
  (100% of 500,000 sampled archive pings and of the live feed).

## 4. Linking GPS points to routes (Stages 2–3)

Instead of matching points to a generic road map, each GPS point is matched to the **bus's own route**, because
the feed says which route the bus is on, and the official GTFS uses the same route IDs (2,191 of 2,203 GPS routes,
99.9% of pings).

1. **Runs.** Each vehicle's points are split whenever `trip_id` changes or there is a gap of more than 15 min.
2. **Trip match.** Candidates are the route's GTFS trips running that day, starting within 30 min of the `HH:MM`
   in the `trip_id` (90 min if none). Start times are compared on a 24-hour circle, because the feed writes
   `00:15` for a GTFS trip at `24:15:00`. Each candidate stop pattern (one per direction) is scored by how many
   of its stops the track passes; the best pattern wins, then the trip with the nearest start.
3. **Stop passing times.** For each stop in order, the first point where the track comes within 50 m, moving
   forward only. The passing time is read off the line between two consecutive GPS points, so a bus that passes a
   stop *between* two reports still gets an accurate time. Times never go backwards.
4. **Filling gaps.** Stops between two observed stops get times interpolated by distance; they are kept for
   completeness but never used to train the model.
5. **Keeping good runs.** Archive runs need ≥ 8 matched stops and ≥ 30% of their pattern (validation uses runs
   with ≥ 90%). Live runs are partial by nature, so the live worker keeps runs with ≥ 4 matched stops; the route
   from the feed still restricts the match to that route's patterns.

**Result (15 days, 7 min on 4 workers):** 43,000–48,000 runs per day matched to a GTFS trip, 1.3–1.5 million stop
passings per day, **92% actually observed**. Each run is matched against the actual 2026 timetable of that day.

## 5. Estimating travel time on each stretch of road (Stage 4)

- **Unit: the stop-to-stop segment.** A segment (stop *a* → stop *b*) is **shared by every route** that serves it,
  so busy corridors collect many observations per hour.
- **Observation.** The time between passing *a* and passing *b*, both actually observed, kept only if the implied
  speed is between 1 and 80 km/h.
- **Dwell.** Time at stops and at signals is *part of* travel time: a segment runs from leaving *a* to leaving
  *b*, so it includes the dwell at *b*. Stationary records from stage 1 keep arrival and departure separately,
  which leaves room for a separate dwell model later.
- **Statistics per (segment, day type, hour).** Median (the estimate) and P85 (a "plan for" time), after trimming
  values beyond 3 × the interquartile range. At query time hourly values are interpolated between hour centres.
- **Little or no data.** Sparse cells are blended towards a coarser estimate:

  T = (n · T_cell + k · T_parent) / (n + k), with k = 8 and confidence = n / (n + k)

  The parent is, in order: the same segment ±1 hour → the same segment and day type, all hours → the same segment
  on all days.
- **Segments never observed.** Distance divided by the city-wide median speed for that day type and hour, so
  every segment always has an estimate, and the API returns a **confidence**.

**Result (13 training days, 15.6 million segment observations):** **12,847 of the 15,119 segments in the GTFS
(85%)** have data. City-wide median segment speed, weekdays:

| Hour | 02:00 | 06:00 | 09:00 | 13:00 | 18:00 | 21:00 | 23:00 |
|---|---|---|---|---|---|---|---|
| km/h | 35 | 27 | 19 | 18 | **15** | 21 | 27 |

Buses are slowest in the **evening peak**. Midday is as slow as 09:00 (on 7,271 well-observed segments, 09:00
takes 0.96 × the 13:00 time); the real contrast is day against night, so the quiet-hour reference for the
"traffic delay" is the faster of 23:00 and 02:00.

## 6. Combining live and historical data

**Live pipeline** (`tools/rt_live.py`, `transit/rt.py`): fetch the GTFS-RT feed every 15 s (0.6 MB, 0.4 s),
keep 3 hours of positions, and every 2 minutes clean them (stage 1 rules) and match them to trips and stops
(stage 2), giving live segment traversals. At 19:29 the window held 5,707 buses, 5,444 matched runs and 89,282
stop passings; a rebuild takes about 5 s.

**Blend** for a ride boarding at time t₀ (`transit/live.py`), using only traversals that finished before t₀:

1. **Corridor factor.** On the segments of the ride's own route, the median ratio observed / historical over
   traversals by **other buses** in the last 60 min: r from n_r observations, shrunk towards 1:
   f = 1 + (r − 1) · n_r / (n_r + 5). This is "this corridor is running x% slower than usual right now".
2. **Own-bus factor** (when the arriving bus is known): its own ratio on the segments it drove in the last 30 min,
   capped to 0.77–1.3, moves the factor: f ← f + (r_o − f) · n_o / (n_o + 20).
3. **Per segment.** T = w · T_live + (1 − w) · f · T_hist(segment, day type, hour), with
   w = n / (n + 1) · e^(−age / 30 min) for a segment traversed n times in the last 20 min.

**Little or no live data:** with no fresh traversal on a segment, w = 0 and the segment uses f · T_hist; with no
live data on the whole route, f = 1 and the answer is the historical model. The answer never depends on live data
being present. All weights were tuned on training days only.

The API uses the live feed for today's date and labels the answer "live feed"; for October dates it replays the
archive the same way, which is how the held-out days are scored (§8).

## 7. Journey planner and API (Stages 6–7)

- **Algorithm: time-dependent RAPTOR**, at most 3 buses. It returns the best arrival for each number of transfers
  plus alternatives (excluding the first bus of options found). Up to 3 options.
- **Times.** Departures from the official GTFS. Each trip's time at every stop adds up model segment times, each
  for the **hour the bus reaches that segment**. Times past midnight are kept as seconds of the service day.
- **Walking.** 1.3 × straight-line distance at 4.5 km/h; stops within 800 m of origin and destination (1.5 km if
  fewer than 3); transfers between stops up to 400 m apart.
- **Each option reports** the walk and bus legs (stops, times, wait, ride time and the timetable's ride time), the
  total, a P85 "plan for" time, the **same trip at a quiet hour** and the resulting **traffic delay**, a
  confidence, and with live data the **live-adjusted total**.
- **Map.** Legs are drawn along the routes' official shapes (`shapes.txt`, cut between stops: 14,573 stretches).
- **API.** `GET /journeys?from=…&to=…&depart=…` (coordinates or stop names, fuzzy matched; `depart` defaults to
  now), `/stops/search`, `/health`, interactive docs at `/docs`, and a Leaflet map page at `/`.
- **Speed.** A query takes about 0.2–0.6 s on the full network (RAPTOR scan compiled with Numba).

**Example: Kashmere Gate ISBT → Nehru Place.**

| Leave (Tuesday) | Best option | Same trip at a quiet hour | Traffic delay |
|---|---|---|---|
| 08:30 | **59.8 min**: 130DOWN, then 425UP | 48.2 min | +11.6 min |
| 13:00 | 75.5 min, 2 transfers | 51.7 min | +23.9 min |
| 18:00 | 77.2 min: 422UP, then 374DOWN | 50.6 min | +26.6 min |
| 23:00 | 62.4 min, including a 6-min wait | 58.6 min | +3.8 min |

With the **live feed on Tue 24 Nov at 19:40**, the same trip shows 69.8 min from history and **67.5 min live**.

## 8. Validation

Full report: `reports/validation_report.pdf`. Summary:

**Held-out test days** (28 and 31 Oct; 1,500 rides between two observed stops ≥ 3 stops apart, from runs with
≥ 90% of stops observed; live blend replayed from the archive with data before boarding only):

| Method | MAE (min) | MAPE | median error | within ±5 min | within ±10% | bias (min) |
|---|---|---|---|---|---|---|
| **Our model + live blend** | **4.48** | **13.8%** | **1.85** | **76.5%** | **51.5%** | −2.53 |
| Our model, historical only | 5.82 | 16.5% | 2.40 | 69.5% | 42.7% | −4.98 |
| Official GTFS timetable | 8.98 | 30.3% | 5.27 | 48.7% | 21.3% | −3.86 |
| Flat city-wide speed | 9.64 | 29.1% | 4.79 | 51.3% | 19.4% | −7.41 |

**Google Maps** (300 of those rides via the Google Maps Routes API, bus-only transit between the same two stops on
the next same weekday and time; Google's in-vehicle bus time; 297 had a bus route):

| Same 297 rides | MAE (min) | MAPE | within ±5 min |
|---|---|---|---|
| **Our model + live blend** | **4.89** | **14.1%** | **77.1%** |
| Our model, historical only | 6.44 | 17.5% | 67.7% |
| Google Maps | 10.16 | 33.6% | 46.1% |
| Official GTFS timetable | 9.65 | 30.6% | 49.2% |

**Live feed, real time** (24 Nov; 1,500 real rides boarding 18:54–19:24, predictions using only data available at
boarding, observed later on the same feed):

| Method | MAE (min) | MAPE | within ±5 min | bias (min) |
|---|---|---|---|---|
| **Our model + live feed** | **2.00** | **19.5%** | **92.9%** | +0.32 |
| Our model, historical only | 2.99 | 28.8% | 83.9% | +1.69 |
| Official GTFS timetable | 3.44 | 28.6% | 75.8% | −1.94 |

**Reading.**
- Our model's error is about **half the timetable's and Google Maps'**: Google's Delhi bus times are as far off as
  the timetable, which suggests they come from the published schedule (not confirmed).
- **Live data cuts the error by a quarter to a third**, on the replayed test days and on the real feed tonight.
  The test days ran slower than the training weeks (history under-predicts by 5 min) and tonight ran faster
  (history over-predicts by 1.7 min); the live blend removes most of either bias.
- By group (historical model, test days): MAE 2.5 / 3.2 / 6.9 / 11.6 min for rides under 2 / 2–5 / 5–15 / 15+ km
  (MAPE 21% / 17% / 16% / 14%); 3.5 min at night, 5.0 in the morning peak, 7.0 at midday, 8.2 in the evening peak;
  4.3 min on the weekday and 7.0 on the Saturday.
- **"Plan for" time:** 83% of test rides arrived within it from history and 91% with live data (target 85%).
- The whole pipeline was first tested on a synthetic network with known answers (model MAE 0.35 min).

## 9. Limitations and next steps

- **One evening of live validation.** The real-time check covers an hour of the feed; a full day, including the
  morning peak, is the next run.
- **Day-to-day variation** is large (test days 5 min slower than training on long rides); the live blend handles
  it. Next: model dwell and layovers separately.
- **Feed coverage.** The live feed currently carries about 5,500 buses on 1,377 routes; the dataset notes that more
  DTC buses will be added. The method needs no change.
- **Waiting time** uses scheduled departures. The live feed's vehicle positions could give real waits for the
  arriving bus.
- **Festival periods** (e.g. Diwali) would need their own day type; older data (1–13 Oct) made the model worse.

## 10. Reproducing

See `README.md`. In short: `pipeline/01_extract.py` (archive) → `bash tools/setup_data.sh` →
`pipeline/02_stop_events.py --train-test` → `04_model.py` → `05_shapes.py` → `08_validate.py`; live:
`tools/rt_live.py`; API: `uvicorn api.main:app`. Every parameter is in `transit/config.py`.
