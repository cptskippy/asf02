"""ASF02 — Python client library for Lulumiao pet slow feeders (OEM model ASF02, sold as CWH01L-A1; BLE JSON-RPC).

Talks the JSON-RPC-over-GATT protocol used by the ASF02 pet feeder family:
connect, bind/unlock, schedule management, on-demand feeding, and status.

Requires `bleak` for BLE. No other runtime dependencies.
"""
from asf02.client import ASF02Client, ASF02Error, ASF02ProtocolError, ASF02LockedError
from asf02.protocol import (
    SERVICE_UUID,
    WRITE_CHAR_UUID,
    NOTIFY_CHAR_UUID,
    decode_advertisement,
    encode_plan_entry,
    decode_plan_entry,
    plan_entry_valid,
    unlock_code_for,
    generate_token,
    decode_plan_slot,
    is_clear_marker,
    PlanEntry,
)
from asf02.discovery import discover_feeders

__version__ = "0.1.0"

__all__ = [
    "ASF02Client",
    "ASF02Error",
    "ASF02ProtocolError",
    "ASF02LockedError",
    "SERVICE_UUID",
    "WRITE_CHAR_UUID",
    "NOTIFY_CHAR_UUID",
    "decode_advertisement",
    "encode_plan_entry",
    "decode_plan_entry",
    "decode_plan_slot",
    "plan_entry_valid",
    "is_clear_marker",
    "unlock_code_for",
    "generate_token",
    "PlanEntry",
    "discover_feeders",
]
