"""
Hongjiali / MAXPOWER product profiles for virtual charger simulation.
Each profile defines hardware specs, OCPP capabilities, and firmware quirks.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class OcppVersion(str, Enum):
    V16 = "1.6"
    V201 = "2.0.1"


class ConnectorType(str, Enum):
    CCS2 = "CCS2"
    CHADEMO = "CHAdeMO"
    TYPE2 = "Type2"


@dataclass
class QuirkConfig:
    """MAXPOWER firmware quirks — toggleable per charger instance.
    Learned from real Maxpower CCS2+CCS2 at Lichtwerk (March 28, 2026)."""
    websocket_ping_interval_zero: bool = True       # WS ping=0, server must ping us
    reject_connection_timeout_change: bool = True    # Reject ChangeConfiguration for ConnectionTimeOut
    stop_transaction_on_reconnect: bool = True       # Send StopTransaction reason=Other on WS reconnect
    meter_values_as_strings: bool = True             # All MeterValues numeric fields as strings
    no_power_measurand: bool = True                  # Does NOT send Power.Active.Import — only Energy, Voltage, Current
    remote_start_needs_cable: bool = True            # Accepts RemoteStart but silently ignores if no car connected
    connector_zero_status: bool = True               # Sends StatusNotification for connector 0 (charger-level)
    authorize_before_start: bool = True              # Sends Authorize after RemoteStart, before StartTransaction

    def as_dict(self) -> dict:
        return {
            "websocket_ping_interval_zero": self.websocket_ping_interval_zero,
            "reject_connection_timeout_change": self.reject_connection_timeout_change,
            "stop_transaction_on_reconnect": self.stop_transaction_on_reconnect,
            "meter_values_as_strings": self.meter_values_as_strings,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QuirkConfig":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# Default MAXPOWER quirks (all enabled)
MAXPOWER_QUIRKS = QuirkConfig()
NO_QUIRKS = QuirkConfig(
    websocket_ping_interval_zero=False,
    reject_connection_timeout_change=False,
    stop_transaction_on_reconnect=False,
    meter_values_as_strings=False,
)


@dataclass(frozen=True)
class ChargerProfile:
    """Hardware profile for a Hongjiali product."""
    name: str
    vendor: str
    model: str
    firmware: str
    max_kw: float
    num_connectors: int
    connector_types: tuple[ConnectorType, ...]
    ocpp_version: OcppVersion
    description: str
    default_quirks: QuirkConfig = field(default_factory=lambda: MAXPOWER_QUIRKS)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "vendor": self.vendor,
            "model": self.model,
            "firmware": self.firmware,
            "max_kw": self.max_kw,
            "num_connectors": self.num_connectors,
            "connector_types": [c.value for c in self.connector_types],
            "ocpp_version": self.ocpp_version.value,
            "description": self.description,
            "default_quirks": self.default_quirks.as_dict(),
        }


# ─── Product Catalog ─────────────────────────────────────────────────────────

PROFILES: dict[str, ChargerProfile] = {
    "ENC-DCL120B": ChargerProfile(
        name="ENC-DCL120B",
        vendor="MAXPOWER",
        model="ENC-DCL120B",
        firmware="DC2_D_V3.10.89",
        max_kw=120.0,
        num_connectors=2,
        connector_types=(ConnectorType.CCS2, ConnectorType.CCS2),
        ocpp_version=OcppVersion.V201,
        description="120kW DC fast charger — production (OCPP 2.0.1)",
    ),
    "ENC-DCL120B-16": ChargerProfile(
        name="ENC-DCL120B-16",
        vendor="MAXPOWER",
        model="ENC-DCL120B",
        firmware="DC2_D_V3.10.89",
        max_kw=120.0,
        num_connectors=2,
        connector_types=(ConnectorType.CCS2, ConnectorType.CCS2),
        ocpp_version=OcppVersion.V16,
        description="120kW DC fast charger — lab/legacy (OCPP 1.6j)",
    ),
    "ENC-DCL060B": ChargerProfile(
        name="ENC-DCL060B",
        vendor="MAXPOWER",
        model="ENC-DCL060B",
        firmware="DC2_D_V3.10.89",
        max_kw=60.0,
        num_connectors=2,
        connector_types=(ConnectorType.CCS2, ConnectorType.CCS2),
        ocpp_version=OcppVersion.V16,
        description="60kW DC fast charger — dual CCS2 (OCPP 1.6j)",
    ),
    "ENC-DCX030A": ChargerProfile(
        name="ENC-DCX030A",
        vendor="MAXPOWER",
        model="ENC-DCX030A",
        firmware="DC2_D_V3.10.89",
        max_kw=30.0,
        num_connectors=1,
        connector_types=(ConnectorType.CCS2,),
        ocpp_version=OcppVersion.V16,
        description="30kW portable DC charger (OCPP 1.6j)",
    ),
}


def get_profile(name: str) -> ChargerProfile:
    """Get a profile by name. Raises KeyError if not found."""
    if name not in PROFILES:
        raise KeyError(f"Unknown profile '{name}'. Available: {list(PROFILES.keys())}")
    return PROFILES[name]


def list_profiles() -> list[dict]:
    """Return all profiles as dicts for API responses."""
    return [p.as_dict() for p in PROFILES.values()]
