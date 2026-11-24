# Live GTFS-RT feed check (Phase 2, step B1)

Fetched 2026-11-24T18:29:24+05:30: 3 snapshots 20 s apart from the endpoint in the approval email (`x-api-key` header).

| Snapshot | Size | Fetch time | Vehicles | Header age |
|---|---|---|---|---|
| 1 | 0.60 MB | 0.48 s | 5,495 | 24.0 s |
| 2 | 0.60 MB | 0.36 s | 5,492 | 7.5 s |
| 3 | 0.60 MB | 0.35 s | 5,492 | 27.9 s |

## Fields filled (last snapshot)

| Field | Filled |
|---|---|
| `vehicle.id` | 5,492 of 5,492 (100%) |
| `vehicle.label` | 5,492 of 5,492 (100%) |
| `trip.trip_id` | 5,492 of 5,492 (100%) |
| `trip.route_id` | 5,492 of 5,492 (100%) |
| `trip.direction_id` | 0 of 5,492 (0%) |
| `trip.start_time` | 5,492 of 5,492 (100%) |
| `trip.start_date` | 5,492 of 5,492 (100%) |
| `position` | 5,492 of 5,492 (100%) |
| `position.speed` | 5,492 of 5,492 (100%) |
| `position.bearing` | 0 of 5,492 (0%) |
| `timestamp` | 5,492 of 5,492 (100%) |
| `stop_id` | 0 of 5,492 (0%) |
| `current_stop_sequence` | 0 of 5,492 (0%) |
| `current_status` | 0 of 5,492 (0%) |
| `congestion_level` | 0 of 5,492 (0%) |
| `occupancy_status` | 0 of 5,492 (0%) |

## Contents

- 5,492 vehicles on 1,377 routes; **1,345** of those routes are in the official static GTFS.
- `trip_id` prefix equals `route_id` for 100% of vehicles (e.g. 3601_16_56_5616, 2875_18_11_7896, 1995_18_04_2954).
- Position age at fetch: median 40.9 s, P90 151.9 s; 1% older than 5 min.
- Between the first and last snapshot (40.8 s), 4,130 of 5,492 vehicles reported a newer position.
