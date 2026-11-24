# Validation report

Train days: 2026-10-14, 2026-10-15, 2026-10-16, 2026-10-17, 2026-10-18, 2026-10-19, 2026-10-21, 2026-10-22, 2026-10-23, 2026-10-24, 2026-10-25, 2026-10-26, 2026-10-27. Test days (held out): 2026-10-28, 2026-10-31.
1500 trip fragments sampled from complete test-day runs (seed 42); each is a real bus ride
between two observed stops, at least 3 stops apart.

## Ride time error by method

| Method | n | MAE (min) | MAPE | P50 (min) | P90 (min) | within ±5 min | within ±10% | bias (min) |
|---|---|---|---|---|---|---|---|---|
| **Our model** (segment × day type × hour) | 1500 | 5.82 | 16.5% | 2.4 | 13.4 | 69.5% | 42.7% | -4.98 |
| **Our model + live blend** (replayed: same-day data from the 20 min before boarding) | 1500 | 4.48 | 13.8% | 1.85 | 9.72 | 76.5% | 51.5% | -2.53 |
| GTFS timetable | 1500 | 8.98 | 30.3% | 5.27 | 19.53 | 48.7% | 21.3% | -3.86 |
| Flat city-wide speed | 1500 | 9.64 | 29.1% | 4.79 | 23.35 | 51.3% | 19.4% | -7.41 |

### Model error by distance

| Group | n | MAE (min) | MAPE | within ±5 min | within ±10% |
|---|---|---|---|---|---|
| <2 km | 147 | 2.5 | 21.4% | 87.8% | 41.5% |
| 2-5 km | 456 | 3.19 | 17.1% | 84.9% | 39.5% |
| 5-15 km | 742 | 6.9 | 15.7% | 62.3% | 43.4% |
| 15+ km | 155 | 11.56 | 13.5% | 41.3% | 49.7% |

### Model error by time of day

| Group | n | MAE (min) | MAPE | within ±5 min | within ±10% |
|---|---|---|---|---|---|
| night/early (0-7) | 179 | 3.54 | 11.8% | 90.5% | 59.8% |
| morning peak (7-11) | 583 | 4.97 | 14.8% | 71.7% | 45.8% |
| midday (11-16) | 293 | 7.0 | 19.0% | 64.2% | 32.4% |
| evening peak (16-21) | 338 | 8.18 | 19.7% | 55.6% | 36.4% |
| late (21-24) | 107 | 3.62 | 16.3% | 80.4% | 44.9% |

### Model error by day type

| Group | n | MAE (min) | MAPE | within ±5 min | within ±10% |
|---|---|---|---|---|---|
| weekday | 632 | 4.25 | 14.7% | 74.5% | 47.5% |
| weekend | 868 | 6.97 | 17.8% | 65.8% | 39.2% |

## "Plan for" time (P85)

Share of rides that arrived within the "plan for" time (target 85%): **83.1%** from history, **91.3%** with the live blend. The buffer over the estimate averages 9.2 min.

Bias > 0 means the method predicts longer rides than observed.
Google Maps comparison: `tools/gmaps_lookup.py` (Routes API) writes `gmaps_comparison.csv` and `gmaps_metrics.json`.
