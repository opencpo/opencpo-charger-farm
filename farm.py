"""
Virtual Charger Farm — ChargerFarm class.
Manages all virtual charger instances, settings, and scenario state.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from profiles import get_profile, QuirkConfig, MAXPOWER_QUIRKS, NO_QUIRKS
from metrics import farm_metrics
from environment import EnvironmentSimulator
from location_push import push_location
from pnc import PnCConfig
from charger16 import VirtualCharger16
from charger201 import VirtualCharger201
from reports import ReportGenerator
from scenarios import BaseScenario

log = logging.getLogger(__name__)

SETTINGS_FILE = Path(__file__).parent / "settings.json"

DEFAULT_SETTINGS = {
    "ocpp16_url": os.environ.get("OCPP16_URL", "ws://localhost:9100/ocpp"),
    "ocpp201_url": os.environ.get("OCPP201_URL", "ws://localhost:9201/ocpp"),
    "cpo_api_url": os.environ.get("CPO_API_URL", ""),
    "cpo_api_key": os.environ.get("CPO_API_KEY", ""),
    "default_profile": "ENC-DCL120B-16",
    "default_quirks_enabled": True,
    "connection_mode": "direct",
    "tailscale_auth_key": "",
    "demo_locations": os.environ.get("DEMO_LOCATIONS", "true").lower() != "false",
    "demo_city": os.environ.get("DEMO_CITY", "Amsterdam"),
}


def load_settings() -> dict:
    import json
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE) as f:
                saved = json.load(f)
                return {**DEFAULT_SETTINGS, **saved}
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    import json
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)


class ChargerFarm:
    """Manages all virtual charger instances."""

    def __init__(self):
        self.chargers: dict[str, VirtualCharger16 | VirtualCharger201] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.settings: dict = load_settings()
        self._spawn_count: int = 0
        self.env_sim = EnvironmentSimulator()
        self.active_scenario: Optional[BaseScenario] = None
        self.scenario_task: Optional[asyncio.Task] = None
        self.report_gen = ReportGenerator(
            cpo_api_url=self.settings.get("cpo_api_url", ""),
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
        version = ocpp_version or profile.ocpp_version.value

        if version == "2.0.1":
            ws_url = self.settings["ocpp201_url"]
            charger = VirtualCharger201(
                cp_id=cp_id, profile=profile, ws_url=ws_url,
                quirks=quirks, site_id=site_id, env_sim=self.env_sim, pnc_config=pnc_config,
            )
        else:
            ws_url = self.settings["ocpp16_url"]
            charger = VirtualCharger16(
                cp_id=cp_id, profile=profile, ws_url=ws_url,
                quirks=quirks, site_id=site_id, env_sim=self.env_sim, pnc_config=pnc_config,
            )

        self.chargers[cp_id] = charger
        self.tasks[cp_id] = asyncio.create_task(self._run_charger(cp_id, charger))

        if self.settings.get("demo_locations", True):
            asyncio.create_task(push_location(
                cp_id=cp_id,
                location_index=self._spawn_count,
                api_url=self.settings.get("cpo_api_url", ""),
                api_key=self.settings.get("cpo_api_key", ""),
                log_event_fn=farm_metrics.log_event,
            ))
        self._spawn_count += 1

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
        connected = charging = discharging = errors = 0
        for charger in self.chargers.values():
            if charger.is_connected:
                connected += 1
            summary = charger.status_summary
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
