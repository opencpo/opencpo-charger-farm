"""
Plug & Charge (ISO 15118) simulation.
Generates fake but structurally valid eMAID tokens, EXI certificate payloads,
and manages the P&C authentication flow timing.
"""

import base64
import hashlib
import logging
import os
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)


class CertStatus(str, Enum):
    VALID = "valid"
    EXPIRED = "expired"
    REVOKED = "revoked"
    UNKNOWN_CA = "unknown_ca"


@dataclass
class PnCConfig:
    """Per-charger Plug & Charge configuration."""
    enabled: bool = False
    emaid_prefix: str = "NL-STM"       # Country-Provider prefix
    contract_id_counter: int = 1
    tls_handshake_delay_sec: float = 2.0   # Realistic delay for TLS + ISO 15118 negotiation
    cert_status: CertStatus = CertStatus.VALID
    iso15118_20: bool = False           # Enable ISO 15118-20 (V2G bidirectional)
    energy_transfer_mode: str = "DC_BidirectionalCharging"

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "emaid_prefix": self.emaid_prefix,
            "tls_handshake_delay_sec": self.tls_handshake_delay_sec,
            "cert_status": self.cert_status.value,
            "iso15118_20": self.iso15118_20,
            "energy_transfer_mode": self.energy_transfer_mode,
        }


def generate_emaid(prefix: str = "NL-STM", counter: int = 1) -> str:
    """
    Generate a structurally valid eMAID (e-Mobility Account Identifier).
    Format: CC-PPP-IIIIII-C (ISO 15118-2)
    CC = country code, PPP = provider, IIIIII = instance, C = check digit
    """
    instance = f"C{counter:05d}"
    base = f"{prefix}-{instance}"
    # Simple check digit (sum of char codes mod 10)
    check = sum(ord(c) for c in base.replace("-", "")) % 10
    return f"{base}-{check}"


def generate_exi_cert_request(emaid: str) -> str:
    """
    Generate a fake but structurally plausible EXI-encoded certificate request.
    In reality this would be ASN.1/DER encoded and then EXI compressed.
    We generate a base64 blob that looks right for testing.
    """
    # Simulate a ~200 byte EXI payload
    payload_parts = [
        b'\x80\x00',                           # EXI header
        b'\x01\x00',                           # schema version
        emaid.encode('ascii'),                  # eMAID
        os.urandom(32),                         # simulated CSR public key hash
        os.urandom(64),                         # simulated CSR signature
        hashlib.sha256(emaid.encode()).digest(), # hash
        os.urandom(48),                         # padding/extensions
    ]
    raw = b''.join(payload_parts)
    return base64.b64encode(raw).decode('ascii')


def generate_contract_cert_response(emaid: str, status: CertStatus = CertStatus.VALID) -> dict:
    """
    Generate a fake contract certificate install response.
    Used for testing CertificateSigned / InstallCertificate flows.
    """
    if status == CertStatus.EXPIRED:
        return {
            "status": "Rejected",
            "statusInfo": {"reasonCode": "CertificateExpired", "additionalInfo": "Contract certificate has expired"},
        }
    elif status == CertStatus.REVOKED:
        return {
            "status": "Rejected",
            "statusInfo": {"reasonCode": "CertificateRevoked", "additionalInfo": "Certificate revoked by issuing CA"},
        }
    elif status == CertStatus.UNKNOWN_CA:
        return {
            "status": "Rejected",
            "statusInfo": {"reasonCode": "NoCertificateAvailable", "additionalInfo": "Unknown CA in chain"},
        }

    # Valid cert chain: Root CA → Sub CA → MO Sub CA → Contract Cert
    cert_chain = []
    for level in ["Root CA", "Sub CA", "MO Sub CA", "Contract"]:
        cert_data = os.urandom(256)
        pem_body = base64.b64encode(cert_data).decode('ascii')
        cert_chain.append(
            f"-----BEGIN CERTIFICATE-----\n{pem_body}\n-----END CERTIFICATE-----"
        )

    return {
        "status": "Accepted",
        "certificate": cert_chain[-1],
        "certChain": cert_chain[:-1],
    }


def generate_signed_meter_value(energy_wh: float, timestamp: str) -> dict:
    """
    Generate a fake signed meter value (MID-certified metering simulation).
    Includes a fake OCMF signature block.
    """
    # OCMF (Open Charge Metering Format) inspired structure
    meter_data = f"OCMF|{{'FV':'1.0','GI':'Virtual Meter','GS':'VIRT-MTR-001'," \
                 f"'GV':'1.0','RD':[{{'TM':'{timestamp}','RV':{energy_wh:.1f}," \
                 f"'RI':'1-0:1.8.0','RU':'Wh','ST':'G'}}]}}"

    # Fake ECDSA signature
    signature = base64.b64encode(os.urandom(72)).decode('ascii')

    return {
        "signedMeterData": meter_data,
        "signingMethod": "ECDSA-secp256r1-SHA256",
        "encodingMethod": "OCMF",
        "publicKey": base64.b64encode(os.urandom(65)).decode('ascii'),
        "signature": signature,
    }


@dataclass
class PnCSession:
    """Tracks state for one P&C authentication flow."""
    emaid: str
    exi_request: str
    cert_status: CertStatus
    started_at: float = field(default_factory=time.monotonic)
    authorized: bool = False
    cert_installed: bool = False
    security_events: list[dict] = field(default_factory=list)

    def log_security_event(self, event_type: str, info: str = "") -> None:
        self.security_events.append({
            "timestamp": time.time(),
            "type": event_type,
            "info": info,
        })
