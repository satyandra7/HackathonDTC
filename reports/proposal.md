# Proposal: the DTC fleet as a traffic sensor

**Team THE HEURISTIC LARPERS** (Satyandra Pal, team lead; Anoushka Malik) · Transport Stack Hackathon 2026 ·
Problem 1: real-time bus traffic model and journey planner · Code: <https://github.com/satyandra7/HackathonDTC>

## The problem

Delhi's bus timetable assumes fixed travel times, but a commuter can't tell whether a trip will take 30 minutes
or 60. Real bus travel times change with the hour, the day and the road. Meanwhile, more than 6,000 DTC buses
report their position about every 11 seconds. That is a city-wide, always-on traffic signal that today is not
used to tell commuters how long their journey will take.

## What we built

A **Google Maps-like bus journey API for Delhi** whose travel times come from how buses actually move:

- `GET /journeys?from=…&to=…&depart=…` takes coordinates or stop names (for example "Kashmere Gate ISBT").
- It returns up to 3 options. Each gives the walk to the first stop, the bus legs, any transfers, the walk to
  the destination, and the total and arrival time.
- Each option also shows:
  - **how long the same trip takes at a quiet hour**, so the traffic delay is visible;
  - a **"plan for" time** (85th percentile);
  - a **confidence** that reflects how much GPS data backs the estimate.
- A **map page** lets anyone try it: type or click the origin and destination, pick a time, compare options.
  Bus legs follow the routes' official road shapes.

## How it works

1. **Clean** a month of GPS positions (913 M rows → 549 M): duplicates, GPS jumps, clock errors.
2. **Match** each bus run to its scheduled trip in the **official 2026 DTC GTFS** (route IDs match the GPS feed
   directly) and turn the GPS track into the time it passed each stop.
3. **Learn** the time for every stop-to-stop **segment**, shared by every route that uses it, by weekday or
   weekend and hour. Sparse cells borrow from neighbouring hours, so every segment always has an estimate.
4. **Go live**: every 2 minutes, the authority's GTFS-RT feed (about 5,500 buses) goes through the same cleaning
   and matching, and fresh segment times from other buses adjust the historical ones.
5. **Plan** with time-dependent RAPTOR: GTFS says *when buses leave*, our model says *how long they take*, for
   the hour the bus reaches each segment, adjusted by the **live feed**.
6. **Validate** on held-out days and on the live feed against real bus trips, the timetable and Google Maps.

## Why it is credible (judging criteria)

| Criterion | How we address it |
|---|---|
| **Problem relevance** | Answers exactly the brief's question: different, traffic-aware times at 9 am and 11 pm, with the quiet-hour comparison from its Kashmere Gate → Nehru Place example |
| **Technical depth and correct use of the data** | Profiled the raw data before designing. Found that the `speed` field doesn't track movement and that feed `trip_id`s aren't GTFS IDs, and designed around both. Follows the GTFS spec: times past 24:00, service calendars, optional files. |
| **Feasibility** | Runs on one machine with DuckDB and Parquet. Cleaning takes 33 s per day, stop matching about 1 min per day, the live feed is reprocessed in about 5 s every 2 minutes, and a journey query takes 0.2–0.6 s at Delhi scale. The whole chain was first tested on synthetic data with known answers. |
| **Innovation** | Segments are shared across routes, so sparse routes learn from busy corridors. Every answer carries its confidence and traffic delay. |
| **Usability and impact** | A simple map anyone can use, and an open API that the Transport Stack or app developers can build on. |

## Early results

All data come from the authority (delhi.transportstack.in): the October 2026 bus GPS archive, the official 2026
static GTFS and the live GTFS-RT feed. Training on 14–27 Oct, testing on the held-out 28 and 31 Oct, and checking
on the live feed on 24 Nov (details: `reports/validation_report.pdf`):

- **Accuracy:** on 1,500 real bus rides from the test days, the error is **4.5 min (14%) with live data** and
  5.8 min (16.5%) from history alone, against **9.0 min (30%)** for the official timetable. 77% of rides are
  within 5 minutes; the timetable manages 49%.
- **Real-time, on the live feed:** on 1,500 real rides on 24 Nov evening, predictions made with the live
  GTFS-RT feed were off by **2.0 min**, against 3.0 min from history alone and 3.4 min for the timetable:
  **a third less error**, using only data available at boarding.
- **Against Google Maps:** on 300 of the test rides, Google Maps' bus ride times are off by **10.2 min (34%)**,
  about as much as the timetable. Ours are off by **6.4 min (18%) from history and 4.9 min (14%) with live
  data**.
- **Coverage:** 2,191 of 2,203 GPS routes in the official GTFS; **12,847 road segments (85%)** learned from
  15.6 million observations; about 5,500 live buses processed every 2 minutes.
- **Traffic signal:** city-wide bus speed falls from 35 km/h at night to **15 km/h in the evening peak**.
  Kashmere Gate → Nehru Place takes 60 min at 08:30 (+12 min of traffic) and 77 min at 18:00 (+27 min).
- **Speed:** a journey query takes about 0.2–0.6 s.

## Next steps

1. **A full day of live validation**, including the morning peak, and continuous monitoring of live accuracy.
2. **All 31 days**, with festival days as their own day type, and dwell time modelled separately by stop and hour.
3. **Real waiting times** from the live position of the bus approaching the boarding stop.
4. **Corridor congestion map**: live speed against free-flow speed, as a city-wide traffic layer.
5. **Packaging** as an open-source Transport Stack component, with documented API and deployment.

## Deliverables

| Deliverable | Where |
|---|---|
| Working prototype: API, map, live worker and pipeline | repository; see `README.md` |
| Source code with README | <https://github.com/satyandra7/HackathonDTC> |
| Technical note | `reports/technical_note.pdf` |
| Validation report | `reports/validation_report.pdf` (data in `reports/results/`) |
| Demo video | [[TBD: link]] |
