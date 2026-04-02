#!/usr/bin/env python3
"""
Virtual Charger Farm — FastAPI Control Plane (routes only).
Main entry point. Run with: uvicorn control:app --host 0.0.0.0 --port 8086
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from farm import ChargerFarm, save_settings
from profiles import MAXPOWER_QUIRKS, NO_QUIRKS
from metrics import farm_metrics
from reports import list_reports, get_report
from scenarios import SCENARIOS, list_scenarios

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ─── App ─────────────────────────────────────────────────────────────────────

farm = ChargerFarm()

app = FastAPI(title="Virtual Charger Farm", version="1.0.0")

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ─── Startup / Shutdown ─────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    farm_metrics.log_event("info", "farm", "Virtual Charger Farm started")
    asyncio.create_task(_metrics_broadcast_loop())


@app.on_event("shutdown")
async def on_shutdown():
    farm_metrics.log_event("info", "farm", "Shutting down...")
    if farm.scenario_task:
        farm.scenario_task.cancel()
    await farm.stop_all()


async def _metrics_broadcast_loop():
    while True:
        await asyncio.sleep(2)
        farm_metrics.broadcast_metrics()


# ─── Web UI ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    index = static_dir / "index.html"
    if index.exists():
        return HTMLResponse(content=index.read_text())
    return HTMLResponse(content="<h1>Virtual Charger Farm</h1><p>UI not found.</p>")


# ─── API: Status ─────────────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    return farm.status()


# ─── API: Chargers ───────────────────────────────────────────────────────────

@app.get("/api/chargers")
async def api_list_chargers():
    return [c.status_summary for c in farm.chargers.values()]


@app.post("/api/chargers")
async def api_spawn_charger(request: Request):
    body = await request.json()
    cp_id = body.get("cp_id")
    profile = body.get("profile", farm.settings.get("default_profile"))
    ocpp_version = body.get("ocpp_version")
    count = body.get("count", 1)
    site_id = body.get("site_id", "default")
    quirks_enabled = body.get("quirks", farm.settings.get("default_quirks_enabled", True))
    pnc = body.get("pnc", False)
    quirks = MAXPOWER_QUIRKS if quirks_enabled else NO_QUIRKS
    spawned = []
    for i in range(count):
        cid = cp_id if count == 1 else f"{cp_id or profile}-{i+1:04d}"
        charger = await farm.spawn_charger(cid, profile, ocpp_version, quirks, site_id, pnc)
        if charger:
            spawned.append(cid)
    return {"spawned": spawned, "count": len(spawned)}


@app.delete("/api/chargers/{cp_id}")
async def api_stop_charger(cp_id: str):
    ok = await farm.stop_charger(cp_id)
    if not ok:
        raise HTTPException(404, f"Charger {cp_id} not found")
    return {"stopped": cp_id}


@app.post("/api/chargers/{cp_id}/start")
async def api_start_session(cp_id: str, request: Request):
    charger = farm.get_charger(cp_id)
    if not charger:
        raise HTTPException(404, f"Charger {cp_id} not found")
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    connector = body.get("connector_id", body.get("evse_id", 1))
    id_tag = body.get("id_tag", "VIRTUAL-TAG")
    ok = await charger.start_charging(connector, id_tag)
    return {"started": ok}


@app.post("/api/chargers/{cp_id}/stop")
async def api_stop_session(cp_id: str, request: Request):
    charger = farm.get_charger(cp_id)
    if not charger:
        raise HTTPException(404, f"Charger {cp_id} not found")
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    connector = body.get("connector_id", body.get("evse_id", 1))
    ok = await charger.stop_charging(connector)
    return {"stopped": ok}


@app.post("/api/chargers/{cp_id}/disconnect")
async def api_disconnect(cp_id: str):
    charger = farm.get_charger(cp_id)
    if not charger:
        raise HTTPException(404, f"Charger {cp_id} not found")
    await charger.force_disconnect()
    return {"disconnected": cp_id}


@app.post("/api/chargers/{cp_id}/reconnect")
async def api_reconnect(cp_id: str):
    charger = farm.get_charger(cp_id)
    if not charger:
        raise HTTPException(404, f"Charger {cp_id} not found")
    await charger.force_reconnect()
    return {"reconnecting": cp_id}


@app.post("/api/chargers/{cp_id}/error")
async def api_inject_error(cp_id: str, request: Request):
    charger = farm.get_charger(cp_id)
    if not charger:
        raise HTTPException(404, f"Charger {cp_id} not found")
    body = await request.json()
    error_code = body.get("error_code", "InternalError")
    connector = body.get("connector", body.get("evse_id", 1))
    await charger.inject_error(error_code, connector)
    return {"injected": error_code}


@app.post("/api/chargers/{cp_id}/v2g")
async def api_v2g(cp_id: str, request: Request):
    charger = farm.get_charger(cp_id)
    if not charger:
        raise HTTPException(404, f"Charger {cp_id} not found")
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    action = body.get("action", "start")
    connector = body.get("connector_id", body.get("evse_id", 1))
    if action == "start":
        ok = await charger.start_v2g(connector, body.get("max_kw", 50), body.get("min_soc", 20))
    else:
        ok = await charger.stop_v2g(connector)
    return {"v2g": action, "ok": ok}


@app.post("/api/chargers/{cp_id}/pnc")
async def api_pnc(cp_id: str, request: Request):
    charger = farm.get_charger(cp_id)
    if not charger:
        raise HTTPException(404, f"Charger {cp_id} not found")
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    connector = body.get("connector_id", body.get("evse_id", 1))
    ok = await charger.trigger_pnc(connector)
    return {"pnc": ok}


# ─── API: Scenarios ──────────────────────────────────────────────────────────

@app.get("/api/scenarios")
async def api_list_scenarios():
    return list_scenarios()


@app.post("/api/scenarios/{name}")
async def api_run_scenario(name: str, request: Request):
    if name not in SCENARIOS:
        raise HTTPException(404, f"Unknown scenario: {name}")
    if farm.active_scenario:
        raise HTTPException(409, f"Scenario '{farm.active_scenario.name}' already running")
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    intensity = body.get("intensity", "medium")
    params = body.get("params", {})
    scenario_cls = SCENARIOS[name]
    scenario = scenario_cls(farm, intensity, **params)
    farm.active_scenario = scenario

    async def run_and_report():
        try:
            result = await scenario.run()
            await farm.report_gen.generate(
                name, result.as_dict() if hasattr(result, "as_dict") else {},
                farm_metrics.snapshot(),
            )
        except Exception as e:
            farm_metrics.log_event("error", f"scenario:{name}", f"Scenario failed: {e}")
        finally:
            farm.active_scenario = None
            farm.scenario_task = None

    farm.scenario_task = asyncio.create_task(run_and_report())
    return {"started": name, "intensity": intensity}


@app.delete("/api/scenarios/active")
async def api_stop_scenario():
    if not farm.active_scenario:
        raise HTTPException(404, "No active scenario")
    farm.active_scenario.cancel()
    if farm.scenario_task:
        farm.scenario_task.cancel()
    name = farm.active_scenario.name
    farm.active_scenario = None
    farm.scenario_task = None
    return {"stopped": name}


# ─── API: Metrics ────────────────────────────────────────────────────────────

@app.get("/api/metrics")
async def api_metrics():
    return farm_metrics.snapshot()


@app.get("/api/metrics/stream")
async def api_metrics_stream():
    async def event_generator():
        q = farm_metrics.subscribe()
        try:
            while True:
                data = await q.get()
                yield f"data: {json.dumps(data, default=str)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            farm_metrics.unsubscribe(q)
    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ─── API: Reports ────────────────────────────────────────────────────────────

@app.get("/api/reports")
async def api_list_reports():
    return list_reports()


@app.get("/api/reports/{report_id}")
async def api_get_report(report_id: str, request: Request):
    fmt = "html" if request.query_params.get("format") == "html" else "json"
    content = get_report(report_id, fmt)
    if content is None:
        raise HTTPException(404, f"Report {report_id} not found")
    if fmt == "html":
        return HTMLResponse(content=content)
    return JSONResponse(content=json.loads(content))


# ─── API: Events ─────────────────────────────────────────────────────────────

@app.get("/api/events")
async def api_events(limit: int = 100):
    return farm_metrics.get_events(limit)


# ─── API: Profiles ───────────────────────────────────────────────────────────

@app.get("/api/profiles")
async def api_profiles():
    from profiles import list_profiles
    return list_profiles()


# ─── API: Settings ───────────────────────────────────────────────────────────

@app.get("/api/settings")
async def api_get_settings():
    return farm.settings


@app.put("/api/settings")
async def api_update_settings(request: Request):
    body = await request.json()
    farm.settings.update(body)
    save_settings(farm.settings)
    farm.report_gen.cpo_api_url = farm.settings.get("cpo_api_url", "")
    return farm.settings


# ─── API: Environment ────────────────────────────────────────────────────────

@app.get("/api/environment")
async def api_environment():
    return farm.env_sim.status()


@app.put("/api/environment")
async def api_update_environment(request: Request):
    body = await request.json()
    if "ambient_temp_c" in body:
        farm.env_sim.site.ambient_temp_c = body["ambient_temp_c"]
    if "grid_voltage_pct" in body:
        farm.env_sim.site.grid_voltage_pct = body["grid_voltage_pct"]
    if "power_available" in body:
        farm.env_sim.site.power_available = body["power_available"]
    if "chaos_intensity" in body:
        farm.env_sim.chaos.enabled = True
        farm.env_sim.chaos.set_intensity(body["chaos_intensity"])
    return farm.env_sim.status()


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    port = int(os.environ.get("PORT", "8086"))
    host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run("control:app", host=host, port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
