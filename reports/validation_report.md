# Validation report: bus travel-time estimates

**Team THE HEURISTIC LARPERS** · Transport Stack Hackathon 2026 · Problem 1 · 24 Nov 2026

> **Summary.** On 1,500 real bus rides from held-out days, our estimates are off by **4.5 min (13.8%)** with live
> data and 5.8 min (16.5%) from history alone. The official timetable is off by 9.0 min (30%) and **Google Maps by
> 10.2 min (34%)**. On the authority's **live GTFS-RT feed**, 1,500 real rides on 24 Nov evening: **2.0 min** with
> the live feed against 3.0 min from history and 3.4 min for the timetable.

All data come from delhi.transportstack.in: the October 2026 bus GPS archive, the official static GTFS and the
live GTFS-RT feed. The method (cleaning, matching, segment model, live blend) is in `technical_note.pdf`.

## 1. What is compared, and how

- **Origin–destination pairs = real bus rides.** A pair is a bus's boarding stop and alighting stop, at least 3
  stops apart, both *actually observed* in the GPS (not interpolated). The **observed trip time** is the time the
  bus took between them. This gives thousands of pairs with a known true answer, across the whole city and day.
- **Sampling.** 1,500 rides at random (seed 42) from runs on the held-out test days (Wed 28 and Sat 31 Oct) with
  ≥ 90% of their stops observed. The model was trained on 14–19 and 21–27 Oct; every setting was tuned on
  training days only, and the test days were scored once.
- **Methods compared**, each predicting the ride time for the same stops and departure time:
  - **our model + live data**: historical model adjusted by GPS from *other* buses in the minutes before boarding
    (on the test days this is replayed from the archive; only data a live feed would have had at boarding);
  - **our model, historical only** (segment × day type × hour);
  - the **official GTFS timetable** for that trip;
  - a **flat city-wide speed**, as a naive baseline;
  - **Google Maps** (§3).
- **Metrics.** Mean absolute error (MAE), mean absolute percentage error (MAPE), median and P90 absolute error,
  share within ±5 min and within ±10%, and bias (mean signed error; positive = predicts too long).

## 2. Held-out test days (28 and 31 Oct, 1,500 rides)

| Method | MAE (min) | MAPE | median | P90 | within ±5 min | within ±10% | bias (min) |
|---|---|---|---|---|---|---|---|
| **Our model + live data** | **4.48** | **13.8%** | **1.85** | **9.72** | **76.5%** | **51.5%** | −2.53 |
| Our model, historical only | 5.82 | 16.5% | 2.40 | 13.40 | 69.5% | 42.7% | −4.98 |
| Official GTFS timetable | 8.98 | 30.3% | 5.27 | 19.53 | 48.7% | 21.3% | −3.86 |
| Flat city-wide speed | 9.64 | 29.1% | 4.79 | 23.35 | 51.3% | 19.4% | −7.41 |

Historical model by group:

| Distance | rides | MAE (min) | MAPE | within ±5 min |
|---|---|---|---|---|
| under 2 km | 147 | 2.50 | 21.4% | 87.8% |
| 2–5 km | 456 | 3.19 | 17.1% | 84.9% |
| 5–15 km | 742 | 6.90 | 15.7% | 62.3% |
| 15 km and more | 155 | 11.56 | 13.5% | 41.3% |

| Time of day | rides | MAE (min) | MAPE | within ±5 min |
|---|---|---|---|---|
| night (0–7) | 179 | 3.54 | 11.8% | 90.5% |
| morning peak (7–11) | 583 | 4.97 | 14.8% | 71.7% |
| midday (11–16) | 293 | 7.00 | 19.0% | 64.2% |
| evening peak (16–21) | 338 | 8.18 | 19.7% | 55.6% |
| late evening (21–24) | 107 | 3.62 | 16.3% | 80.4% |

| Day | rides | MAE (min) | MAPE | within ±5 min |
|---|---|---|---|---|
| Wednesday 28 Oct | 632 | 4.25 | 14.7% | 74.5% |
| Saturday 31 Oct | 868 | 6.97 | 17.8% | 65.8% |

**"Plan for" time** (the P85 time the API shows for planning): 83% of rides arrived within it from history alone
and 91% with live data (target 85%).

**Reading.** Errors grow with distance in minutes but fall in percent. Night and late evening are the most
predictable; midday and the evening peak vary most from day to day. The test days ran slower than the training
weeks (history under-predicts by 5 min); the live data sees that and halves the bias.

## 3. Google Maps

- **How.** 300 of the 1,500 rides were sent to the **Google Maps Routes API** (`tools/gmaps_lookup.py`) as transit
  trips restricted to bus, between the same two stops' coordinates, departing on the **next same weekday at the
  same time** (Google only plans ahead: Wed 25 and Sat 28 Nov). Google's **bus ride time** is its in-vehicle time
  (stop departure to arrival), comparable to the observed ride, which excludes walking and waiting. Google
  returned a bus route for 297 of the 300.
- **Result (same 297 rides):**

| Method | MAE (min) | MAPE | within ±5 min | bias (min) |
|---|---|---|---|---|
| **Our model + live data** | **4.89** | **14.1%** | **77.1%** | −2.96 |
| Our model, historical only | 6.44 | 17.5% | 67.7% | −5.68 |
| Google Maps (bus ride time) | 10.16 | 33.6% | 46.1% | −3.42 |
| Official GTFS timetable | 9.65 | 30.6% | 49.2% | −4.63 |

  On the 270 rides where Google also proposes a single bus: Google 9.37 min, ours 5.97 (history) and 4.56 (live).
- **Reading.** Google's Delhi bus times are about as far off as the timetable, which suggests they come from the
  published schedule (not confirmed from outside). Learning from the buses' GPS cuts that error by about **37%**,
  and with live data by about **52%**.
- **Caveats.** Google forecasts a date four weeks later than the rides; it may choose a different bus line between
  the same two stops. Data: `reports/results/gmaps_comparison.csv` (one row per ride, with Google's lines).

## 4. Real time, on the authority's live feed (24 Nov)

- **Feed.** GTFS-RT VehiclePositions from delhi.transportstack.in (dataset 7): about 5,500 buses, positions 41 s
  old on median. Our live worker fetches it every 15 s and rebuilds live stop passings every 2 minutes.
- **How.** 1,500 real rides seen on the live feed, boarding 18:54–19:24 IST (at least 25 min after the worker
  started, so the live window was full). Each prediction uses **only data available at boarding**: the
  historical model, and the live blend from other buses (and the bus's own earlier stretches). The observed time
  comes from the same feed, after the ride. Median ride 5 stops, 11.2 min.

| Method | MAE (min) | MAPE | median | within ±5 min | within ±10% | bias (min) |
|---|---|---|---|---|---|---|
| **Our model + live feed** | **2.00** | **19.5%** | **1.23** | **92.9%** | **45.5%** | +0.32 |
| Our model, historical only | 2.99 | 28.8% | 1.78 | 83.9% | 32.2% | +1.69 |
| Official GTFS timetable | 3.44 | 28.6% | 2.49 | 75.8% | 21.9% | −1.94 |

| Boarding | rides | historical MAE | live MAE |
|---|---|---|---|
| 18:30–19:00 | 474 | 3.84 | 2.60 |
| 19:00–19:30 | 1,026 | 2.60 | 1.72 |

- **Reading.** That evening buses ran faster than the October model expects (+1.7 min per ride from history); the
  live feed removes most of the bias and cuts the error by **a third**, in both half-hours, in line with the
  replayed test days (−23%).
- **Caveats.** One hour of one evening; rides are shorter than in §2 because the live window was young, so the
  absolute errors are not comparable with §2.

## 5. Data files

| File | Content |
|---|---|
| `reports/results/validation_samples.csv` | the 1,500 test rides: stops, time, observed, each method's prediction |
| `reports/results/validation.md`, `validation_metrics.json` | the full tables of §2 |
| `reports/results/gmaps_comparison.csv`, `gmaps_metrics.json` | the 300 Google Maps lookups of §3 |
| `reports/results/live_check.md`, `live_check.json` | the live-feed check of §4 |
| `reports/results/rt_feed_check.md` | what the live feed contains |
