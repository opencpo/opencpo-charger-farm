#!/usr/bin/env python3
"""
Virtual Charger Farm — FastAPI Control Plane.
Main entry point. Run with: uvicorn control:app --host 0.0.0.0 --port 8086
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from profiles import PROFILES, get_profile, list_profiles, QuirkConfig, OcppVersion, MAXPOWER_QUIRKS, NO_QUIRKS
from metrics import farm_metrics
from environment import EnvironmentSimulator
from pnc import PnCConfig
from charger16 import VirtualCharger16
from charger201 import VirtualCharger201
from scenarios import SCENARIOS, list_scenarios, BaseScenario
from reports import ReportGenerator, list_reports, get_report

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ─── Settings ────────────────────────────────────────────────────────────────

SETTINGS_FILE = Path(__file__).parent / "settings.json"

DEFAULT_SETTINGS = {
    "ocpp16_url": os.environ.get("OCPP16_URL", "ws://localhost:9100/ocpp"),
    "ocpp201_url": os.environ.get("OCPP201_URL", "ws://localhost:9201/ocpp"),
    "cpo_api_url": os.environ.get("CPO_API_URL", ""),
    "redis_host": os.environ.get("REDIS_HOST", ""),
    "default_profile": "ENC-DCL120B-16",
    "default_quirks_enabled": True,
    "connection_mode": "direct",
    "tailscale_auth_key": "",
}


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE) as f:
                saved = json.load(f)
                return {**DEFAULT_SETTINGS, **saved}
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


# ─── Charger Farm ────────────────────────────────────────────────────────────

class ChargerFarm:
    """Manages all virtual charger instances."""

    def __init__(self):
        self.chargers: dict[str, VirtualCharger16 | VirtualCharger201] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.settings: dict = load_settings()
        self.env_sim = EnvironmentSimulator()
        self.active_scenario: Optional[BaseScenario] = None
        self.scenario_task: Optional[asyncio.Task] = None
        self.report_gen = ReportGenerator(
            cpo_api_url=self.settings.get("cpo_api_url", ""),
            redis_host=self.settings.get("redis_host", ""),
        )

    def get_charger(self, cp_id: str):
        return self.chargers.get(cp_id)

    async def spawn_charger(
        self,
        cp_id: str,
        profile_name: str = None,
        ocpp_version: str = None,
        quirks: Optional[QuirkConfig] = None,
        site_id: str = "default",
        pnc_enabled: bool = False,
    ) -> Optional[object]:
        if cp_id in self.chargers:
            return self.chargers[cp_id]

        profile_name = profile_name or self.settings.get("default_profile", "ENC-DCL120B-16")
        try:
            profile = get_profile(profile_name)
        except KeyError:
            return None

        if quirks is None:
            quirks = MAXPOWER_QUIRKS if self.settings.get("default_quirks_enabled", True) else NO_QUIRKS

        pnc_config = PnCConfig(enabled=pnc_enabled)

        # Determine OCPP version from profile or override
        version = ocpp_version or profile.ocpp_version.value

        if version == "2.0.1":
            ws_url = self.settings["ocpp201_url"]
            charger = VirtualCharger201(
                cp_id=cp_id,
                profile=profile,
                ws_url=ws_url,
                quirks=quirks,
                site_id=site_id,
                env_sim=self.env_sim,
                pnc_config=pnc_config,
            )
        else:
            ws_url = self.settings["ocpp16_url"]
            charger = VirtualCharger16(
                cp_id=cp_id,
                profile=profile,
                ws_url=ws_url,
                quirks=quirks,
                site_id=site_id,
                env_sim=self.env_sim,
                pnc_config=pnc_config,
            )

        self.chargers[cp_id] = charger
        self.tasks[cp_id] = asyncio.create_task(self._run_charger(cp_id, charger))
        farm_metrics.log_event("info", "farm", f"Spawned {cp_id} ({profile_name}, OCPP {version})")
        return charger

    async def _run_charger(self, cp_id: str, charger):
        try:
            await charger.start()
        except Exception as e:
            farm_metrics.log_event("error", cp_id, f"Charger crashed: {e}")
            farm_metrics.record_error()
        finally:
            self.chargers.pop(cp_id, None)
            self.tasks.pop(cp_id, None)

    async def stop_charger(self, cp_id: str) -> bool:
        charger = self.chargers.get(cp_id)
        if not charger:
            return False
        await charger.stop()
        task = self.tasks.pop(cp_id, None)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.chargers.pop(cp_id, None)
        farm_metrics.log_event("info", "farm", f"Stopped {cp_id}")
        return True

    async def stop_all(self):
        for cp_id in list(self.chargers.keys()):
            await self.stop_charger(cp_id)

    def status(self) -> dict:
        total = len(self.chargers)
        connected = 0
        charging = 0
        discharging = 0
        errors = 0

        for charger in self.chargers.values():
            if charger.is_connected:
                connected += 1
            summary = charger.status_summary
            # Check connectors/evses for charging state
            conns = summary.get("connectors", summary.get("evses", {}))
            for c in conns.values():
                if c.get("direction") == "charging":
                    charging += 1
                elif c.get("direction") == "discharging":
                    discharging += 1
                if c.get("status") in ("Faulted", "faulted"):
                    errors += 1

        metrics = farm_metrics.snapshot()
        return {
            "total_chargers": total,
            "connected": connected,
            "charging": charging,
            "discharging": discharging,
            "errors": errors,
            "messages_per_sec": metrics["messages_per_sec"],
            "avg_latency_ms": metrics["avg_latency_ms"],
            "scenario_active": self.active_scenario.name if self.active_scenario else None,
            "environment": self.env_sim.status(),
        }


# ─── FastAPI App ─────────────────────────────────────────────────────────────

farm = ChargerFarm()

app = FastAPI(title="Virtual Charger Farm", version="1.0.0")

# Static files
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ─── Startup / Shutdown ─────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    farm_metrics.log_event("info", "farm", "Virtual Charger Farm started")
    # Start metrics broadcast loop
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
    return HTMLResponse(content="<h1>Virtual Charger Farm</h1><p>UI not found. Place index.html in static/</p>")


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
            # Generate report
            await farm.report_gen.generate(
                name, result.as_dict() if hasattr(result, 'as_dict') else {},
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
    # Update report generator
    farm.report_gen.cpo_api_url = farm.settings.get("cpo_api_url", "")
    farm.report_gen.redis_host = farm.settings.get("redis_host", "")
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
    uvicorn.run(
        "control:app",
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
