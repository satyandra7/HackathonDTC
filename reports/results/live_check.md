# Real-time check on the live GTFS-RT feed (Phase 2, step B6)

1,500 real bus rides on 2026-11-24, boarding 18:54–19:24 IST (live feed from 18:29); median 5 stops, 11.2 min. Predictions use only data available at boarding.

| Method | MAE (min) | MAPE | median error | within ±5 min | within ±10% | bias (min) |
|---|---|---|---|---|---|---|
| **Our model + live feed** | 2.0 | 19.5% | 1.23 | 92.9% | 45.5% | 0.32 |
| Our model, historical only | 2.99 | 28.8% | 1.78 | 83.9% | 32.2% | 1.69 |
| GTFS timetable (2026) | 3.44 | 28.6% | 2.49 | 75.8% | 21.9% | -1.94 |

| Boarding | rides | historical MAE | live MAE |
|---|---|---|---|
| 18:30 | 474 | 3.84 | 2.6 |
| 19:00 | 1026 | 2.6 | 1.72 |
