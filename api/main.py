"""HTTP API and map page (technical note, section on the planner and API).

Run:  .venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000
Docs: http://localhost:8000/docs     Map: http://localhost:8000/
"""

from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from transit import config as C  # noqa: E402
from transit.planner import Network  # noqa: E402

IST = dt.timezone(dt.timedelta(seconds=C.IST_OFFSET_S))
app = FastAPI(title="Delhi bus journey planner",
              description="Bus journeys with travel times learned from DTC GPS data (Transport Stack Hackathon 2026, Problem 1).")
_net: Network | None = None
_loaded_at = 0.0


def net() -> Network:
    global _net, _loaded_at
    if _net is None:
        t0 = time.time()
        _net = Network()
        _loaded_at = time.time() - t0
    return _net


@app.on_event("startup")
def _startup() -> None:
    net()


@app.get("/health")
def health():
    n = net()
    return {"status": "ok", "gtfs": n.g.summary(), "model_loaded": n.model_loaded,
            "segments_in_model": int(len(n.seg_est)), "load_seconds": round(_loaded_at, 1)}


@app.get("/stops/search")
def stops_search(q: str = Query(..., min_length=2), limit: int = Query(10, le=50)):
    return net().search_stops(q, limit)


@app.get("/journeys")
def journeys(
    from_: str = Query(..., alias="from", description="'lat,lon' or a stop name, e.g. 'Kashmere Gate ISBT'"),
    to: str = Query(..., description="'lat,lon' or a stop name"),
    depart: str | None = Query(None, description="ISO date-time, e.g. 2026-11-24T08:30+05:30 (default: now, IST)"),
    max_options: int = Query(C.MAX_OPTIONS, ge=1, le=5),
):
    try:
        when = dt.datetime.fromisoformat(depart) if depart else dt.datetime.now(IST)
    except ValueError:
        raise HTTPException(400, "depart must be an ISO date-time, e.g. 2026-11-24T08:30+05:30")
    if when.tzinfo is None:
        when = when.replace(tzinfo=IST)
    t0 = time.time()
    try:
        res = net().journeys(from_, to, when, max_options)
    except ValueError as e:
        raise HTTPException(404, str(e))
    res["compute_ms"] = round((time.time() - t0) * 1000)
    return res


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(Path(__file__).resolve().parent.parent / "web" / "index.html")
