#!/usr/bin/env python3
"""
Virtual Charger Farm — Web UI (optional, development only).
NOT the primary entry point. The production entry point is control.py (port 8086).

Run web UI with: uvicorn main:app --host 127.0.0.1 --port 8087
Run production with: uvicorn control:app --host 0.0.0.0 --port 8086
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

# ─── OCPP library compatibility shim ─────────────────────────────────────────
# Newer ocpp versions renamed some enums — patch before importing charger files
import ocpp.v201.enums as _v201_enums
import ocpp.v201.call_result as _v201_cr
import ocpp.v16.enums as _v16_enums

def _alias(module, old_name, new_name):
    if not hasattr(module, old_name) and hasattr(module, new_name):
        setattr(module, old_name, getattr(module, new_name))

_alias(_v201_enums, "TriggerMessageType", "MessageTriggerType")
_alias(_v201_enums, "DataTransferStatusType", "DataTransferStatus")
_alias(_v201_enums, "ReservationUpdateStatusType", "ReserveNowStatusType")

from profiles import PROFILES, get_profile, list_profiles, MAXPOWER_QUIRKS, NO_QUIRKS, QuirkConfig
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

BASE_DIR = Path(__file__).parent
SETTINGS_FILE = BASE_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "ocpp16_url": os.environ.get("OCPP16_URL", "ws://127.0.0.1:9100/ocpp"),
    "ocpp201_url": os.environ.get("OCPP201_URL", "ws://127.0.0.1:9201/ocpp"),
    "cpo_api_url": os.environ.get("CPO_API_URL", ""),
    "redis_host": os.environ.get("REDIS_HOST", ""),
    "default_profile": "ENC-DCL120B-16",
    "default_quirks_enabled": True,
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


# ─── Farm State ──────────────────────────────────────────────────────────────

class FarmState:
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
        self.running: bool = False
        self.paused: bool = False
        self.farm_config: dict = {
            "count": 10,
            "ocpp_version": "1.6",
            "profile": "ENC-DCL120B-16",
            "ocpp_url": self.settings["ocpp16_url"],
        }
        # Condition config
        self.conditions: dict = {
            "latency_ms": 0,
            "packet_loss_pct": 0,
            "disconnect_interval_s": 0,
            "fault_types": [],
            "fault_probability": 0,
            "fault_target": "all",
            "fault_target_ids": "",
            "auto_sessions": False,
            "soc_min": 20,
            "soc_max": 80,
            "session_duration_min": 30,
            "concurrent_sessions": 1,
            "accept_smart_charging": True,
            "smart_charging_delay_s": 0,
            "smart_charging_override_pct": 100,
            "pnc_enabled": False,
            "pnc_cert_validity": "valid",
        }

    def get_charger(self, cp_id: str):
        return self.chargers.get(cp_id)

    async def spawn_charger(self, cp_id: str, profile_name: str = None,
                             ocpp_version: str = None, quirks: Optional[QuirkConfig] = None,
                             site_id: str = "default", pnc_enabled: bool = False):
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
        version = ocpp_version or profile.ocpp_version.value
        if version == "2.0.1":
            ws_url = self.settings["ocpp201_url"]
            charger = VirtualCharger201(cp_id=cp_id, profile=profile, ws_url=ws_url,
                                         quirks=quirks, site_id=site_id, env_sim=self.env_sim, pnc_config=pnc_config)
        else:
            ws_url = self.settings["ocpp16_url"]
            charger = VirtualCharger16(cp_id=cp_id, profile=profile, ws_url=ws_url,
                                        quirks=quirks, site_id=site_id, env_sim=self.env_sim, pnc_config=pnc_config)
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
        self.running = False
        self.paused = False

    def status_summary(self) -> dict:
        total = len(self.chargers)
        connected = charging = errors = 0
        for charger in self.chargers.values():
            if charger.is_connected:
                connected += 1
            summary = charger.status_summary
            conns = summary.get("connectors", summary.get("evses", {}))
            for c in conns.values():
                if c.get("direction") == "charging":
                    charging += 1
                if c.get("status") in ("Faulted", "faulted"):
                    errors += 1
        metrics = farm_metrics.snapshot()
        return {
            "running": self.running,
            "paused": self.paused,
            "total_chargers": total,
            "connected": connected,
            "charging": charging,
            "errors": errors,
            "messages_per_sec": metrics["messages_per_sec"],
            "avg_latency_ms": metrics["avg_latency_ms"],
            "total_errors": metrics["total_errors"],
            "scenario_active": self.active_scenario.name if self.active_scenario else None,
        }


# ─── App ─────────────────────────────────────────────────────────────────────

farm = FarmState()
app = FastAPI(title="Charger Farm Manager", version="2.0.0")

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.on_event("startup")
async def on_startup():
    farm_metrics.log_event("info", "farm", "Charger Farm Manager started — waiting for user to start farm")
    asyncio.create_task(_metrics_loop())


@app.on_event("shutdown")
async def on_shutdown():
    if farm.scenario_task:
        farm.scenario_task.cancel()
    await farm.stop_all()


async def _metrics_loop():
    while True:
        await asyncio.sleep(2)
        farm_metrics.broadcast_metrics()


# ─── Web UI ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    profiles = list_profiles()
    scenarios = list_scenarios()
    return templates.TemplateResponse("farm.html", {
        "request": request,
        "profiles": profiles,
        "scenarios": scenarios,
        "farm": farm.status_summary(),
        "conditions": farm.conditions,
        "farm_config": farm.farm_config,
    })


# ─── API: Farm Control ────────────────────────────────────────────────────────

@app.post("/api/farm/start")
async def api_farm_start(request: Request):
    body = await request.json()
    count = int(body.get("count", farm.farm_config["count"]))
    ocpp_version = body.get("ocpp_version", farm.farm_config["ocpp_version"])
    profile_name = body.get("profile", farm.farm_config["profile"])
    ocpp_url = body.get("ocpp_url", "")
    if ocpp_url:
        if ocpp_version in ("2.0.1",):
            farm.settings["ocpp201_url"] = ocpp_url
        else:
            farm.settings["ocpp16_url"] = ocpp_url
    farm.farm_config.update({"count": count, "ocpp_version": ocpp_version, "profile": profile_name})
    pnc = farm.conditions.get("pnc_enabled", False)
    spawned = []
    for i in range(count):
        cp_id = f"FARM-{profile_name}-{i+1:04d}"
        version = ocpp_version if ocpp_version != "mixed" else ("2.0.1" if i % 2 == 0 else "1.6")
        charger = await farm.spawn_charger(cp_id, profile_name, version, pnc_enabled=pnc)
        if charger:
            spawned.append(cp_id)
    farm.running = True
    farm.paused = False
    farm_metrics.log_event("info", "farm", f"Farm started: {len(spawned)} chargers (OCPP {ocpp_version})")
    return {"started": len(spawned), "chargers": spawned}


@app.post("/api/farm/stop")
async def api_farm_stop():
    count = len(farm.chargers)
    await farm.stop_all()
    farm_metrics.log_event("info", "farm", f"Farm stopped ({count} chargers)")
    return {"stopped": count}


@app.post("/api/farm/pause")
async def api_farm_pause():
    farm.paused = not farm.paused
    farm_metrics.log_event("info", "farm", f"Farm {'paused' if farm.paused else 'resumed'}")
    return {"paused": farm.paused}


@app.get("/api/farm/status")
async def api_farm_status():
    return farm.status_summary()


@app.post("/api/farm/config")
async def api_farm_config(request: Request):
    body = await request.json()
    farm.conditions.update(body)
    return {"ok": True, "conditions": farm.conditions}


# ─── API: Metrics ────────────────────────────────────────────────────────────

@app.get("/api/metrics")
async def api_metrics():
    return farm_metrics.snapshot()


@app.post("/api/metrics/reset")
async def api_metrics_reset():
    """Reset all counters and event log."""
    farm_metrics.reset_counters()
    return {"ok": True, "message": "All counters reset"}


@app.get("/api/events/stream")
async def api_events_stream():
    async def gen():
        q = farm_metrics.subscribe()
        try:
            while True:
                data = await q.get()
                yield f"data: {json.dumps(data, default=str)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            farm_metrics.unsubscribe(q)
    return StreamingResponse(gen(), media_type="text/event-stream")


# ─── API: Chargers ────────────────────────────────────────────────────────────

@app.get("/api/chargers")
async def api_chargers():
    return [c.status_summary for c in farm.chargers.values()]


@app.post("/api/charger/{cp_id}/fault")
async def api_inject_fault(cp_id: str, request: Request):
    charger = farm.get_charger(cp_id)
    if not charger:
        raise HTTPException(404, f"Charger {cp_id} not found")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    error_code = body.get("error_code", "InternalError")
    connector = body.get("connector", 1)
    await charger.inject_error(error_code, connector)
    return {"injected": error_code, "cp_id": cp_id}


@app.post("/api/charger/{cp_id}/disconnect")
async def api_charger_disconnect(cp_id: str):
    charger = farm.get_charger(cp_id)
    if not charger:
        raise HTTPException(404, f"Charger {cp_id} not found")
    await charger.force_disconnect()
    return {"disconnected": cp_id}


@app.post("/api/charger/{cp_id}/session/start")
async def api_session_start(cp_id: str, request: Request):
    charger = farm.get_charger(cp_id)
    if not charger:
        raise HTTPException(404, f"Charger {cp_id} not found")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    connector = body.get("connector_id", 1)
    id_tag = body.get("id_tag", "FARM-TAG")
    ok = await charger.start_charging(connector, id_tag)
    return {"started": ok}


@app.post("/api/charger/{cp_id}/session/stop")
async def api_session_stop(cp_id: str, request: Request):
    charger = farm.get_charger(cp_id)
    if not charger:
        raise HTTPException(404, f"Charger {cp_id} not found")
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    connector = body.get("connector_id", 1)
    ok = await charger.stop_charging(connector)
    return {"stopped": ok}


# ─── API: Scenarios ──────────────────────────────────────────────────────────

@app.get("/api/scenarios")
async def api_list_scenarios():
    return list_scenarios()


@app.post("/api/scenario/{name}/start")
async def api_scenario_start(name: str, request: Request):
    if name not in SCENARIOS:
        raise HTTPException(404, f"Unknown scenario: {name}")
    if farm.active_scenario:
        raise HTTPException(409, f"Scenario '{farm.active_scenario.name}' already running")
    # Auto-start farm if not running
    if not farm.running:
        await api_farm_start(request)
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    intensity = body.get("intensity", "medium")
    scenario_cls = SCENARIOS[name]
    scenario = scenario_cls(farm, intensity)
    farm.active_scenario = scenario

    async def _run():
        try:
            await scenario.run()
        except Exception as e:
            farm_metrics.log_event("error", f"scenario:{name}", str(e))
        finally:
            farm.active_scenario = None
            farm.scenario_task = None

    farm.scenario_task = asyncio.create_task(_run())
    return {"started": name, "intensity": intensity}


# ─── API: Reconnect Storm ────────────────────────────────────────────────────

@app.post("/api/farm/reconnect-storm")
async def api_reconnect_storm():
    async def _storm():
        for cp_id, charger in list(farm.chargers.items()):
            await charger.force_disconnect()
        await asyncio.sleep(1)
        for cp_id, charger in list(farm.chargers.items()):
            await charger.force_reconnect()
    asyncio.create_task(_storm())
    farm_metrics.log_event("warning", "farm", "Reconnect storm triggered")
    return {"ok": True}


def main():
    uvicorn.run("main:app", host="127.0.0.1", port=8087, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
