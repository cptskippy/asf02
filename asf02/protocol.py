"""ASF02 feeder wire protocol: constants, framing, and pure codecs.

Everything here is pure (no I/O, no asyncio) so it can be unit-tested against
captured wire traffic. Live-verified against a real ASF02 unit (firmware
1.19.12).

Protocol summary
----------------
JSON-RPC-ish messages over two GATT characteristics of service ``55535343-...``:

  request  (written, UTF-8):  {"m":"<method>","i":<id>,"p":<params>}
  response (notified, UTF-8): {"i":<id>,"r":<result>}   or   {"i":<id>,"e":{"c":<code>,"m":"<msg>"}}

Responses do NOT echo the method; matching is by ``i``. The device is locked
across every (re)connect; only ``info`` works while locked. ``feeder.log`` may
return no notification at all (treat timeout as empty).
"""
from __future__ import annotations

import hashlib
import secrets
import string
from dataclasses import dataclass

SERVICE_UUID = "55535343-fe7d-4ae5-8fa9-9fafd205e455"
WRITE_CHAR_UUID = "49535343-8841-43f4-a8d4-ecbe34729bb3"
NOTIFY_CHAR_UUID = "49535343-1e4d-4bd9-ba61-23c647249616"
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"

# Advertising: manufacturer data (company id 0xFFFF) layout:
#   0203 0010 aabbccddeeff
#   ^^^^ ^^^^ ^^^^^^^^^^^^
#   type flags   MAC of the device (12 hex chars)
ADV_COMPANY_ID = 0xFFFF
ADV_TYPE_PREFIX = b"\x02\x03"  # pet feeder
PLAN_SLOT_COUNT = 10  # device-side fixed schedule array
CLEAR_MARKER = "0000000000"

# Known error codes (live-captured)
ERR_DEVICE_LOCKED = -31400
ERR_INVALID_PARAMS = -32602
ERR_LOCK_STATE_2 = -31402  # app reacts; semantics unconfirmed
ERR_LOCK_STATE_4 = -31404  # app reacts; semantics unconfirmed

_TOKEN_ALPHABET = string.ascii_lowercase + string.ascii_uppercase + string.digits


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ASF02Error(Exception):
    """Base class for ASF02 protocol errors (carries the device error payload)."""

    def __init__(self, code: int, message: str):
        super().__init__(f"device error {code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Token / unlock
# ---------------------------------------------------------------------------
def generate_token(n: int = 10) -> str:
    """Generate a random bind token (same charset the vendor app uses)."""
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(n))


def unlock_code_for(token: str) -> str:
    """Compute the unlock code for a bind token.

    Mirrors the vendor app: ``BigInteger(1, md5("0000:" + token)).toString(16)``
    — hex of the digest as an unsigned big-endian integer, so leading zero
    nibbles are dropped. A plain ``hexdigest()`` will NOT match when the digest
    starts with a 0x0x byte.

    >>> unlock_code_for("ExampleTok1")
    'a587ea45721f8a612c0fcbf507d69adc'
    """
    digest = hashlib.md5(f"0000:{token}".encode("utf-8")).digest()
    return format(int.from_bytes(digest, "big"), "x")


# ---------------------------------------------------------------------------
# Plan strings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PlanEntry:
    """One schedule slot: time (hour, minute), portion count, enabled flag.

    ``portion`` = how many auger revolutions to dispense (one revolution
    is one portion).
    """

    hour: int
    minute: int
    portion: int  # 1..255 (vendor UI offers a small subset)
    enabled: bool


def encode_plan_entry(hour: int, minute: int, portion: int, enabled: bool = True) -> str:
    """Encode a schedule entry as the device's 10-char hex string.

    Layout: ``"00" + HH + MM + PP + XX`` (HH/MM/PP hex, XX enable flag).
    """
    if not 0 <= hour <= 23:
        raise ValueError(f"hour out of range: {hour}")
    if not 0 <= minute <= 59:
        raise ValueError(f"minute out of range: {minute}")
    if not 0 <= portion <= 255:
        raise ValueError(f"portion out of range: {portion}")
    return f"00{hour:02x}{minute:02x}{portion:02x}{0x01 if enabled else 0x00:02x}"


def decode_plan_entry(entry: str) -> PlanEntry:
    """Decode a 10-char hex plan string into a :class:`PlanEntry`."""
    if not plan_entry_valid(entry):
        raise ValueError(f"malformed plan entry: {entry!r}")
    return PlanEntry(
        hour=int(entry[2:4], 16),
        minute=int(entry[4:6], 16),
        portion=int(entry[6:8], 16),
        enabled=entry[8:10] == "01",
    )


def plan_entry_valid(entry: str) -> bool:
    """True if *entry* is a well-formed 10-char plan string (any time/portion)."""
    if len(entry) != 10 or entry[0:2] != "00":
        return False
    try:
        int(entry, 16)
    except ValueError:
        return False
    return not (0 <= int(entry[2:4], 16) > 23) and not (0 <= int(entry[4:6], 16) > 59)


def is_clear_marker(entry: str) -> bool:
    """True for the all-zeros entry, which the device treats as 'clear schedule'."""
    return entry == CLEAR_MARKER


def decode_plan_slot(slot: str) -> PlanEntry | None:
    """Decode one slot of the 10-slot array.

    The device returns ``""`` for unused slots and ``"0000000000"`` as a clear
    marker; both map to ``None`` here.
    """
    if not slot or is_clear_marker(slot):
        return None
    return decode_plan_entry(slot)


# ---------------------------------------------------------------------------
# Advertising
# ---------------------------------------------------------------------------
def decode_advertisement(manufacturer_data: bytes, local_name: str | None = None) -> dict:
    """Decode a feeder's advertising payload.

    ``manufacturer_data`` is the *value* of the manufacturer-specific data
    field (without the company-id prefix). Returns a dict with:

    - ``product_type``: first 2 bytes (``b'\\x02\\x03'`` = pet feeder)
    - ``mac``: the device MAC (last 6 bytes), e.g. ``"AA:BB:CC:DD:EE:FF"``
    - ``model``: from the local name (``ASF02-DDEEFF`` -> ``"ASF02"``) or None

    Raises ``ValueError`` if the payload doesn't look like a feeder ad.
    """
    if len(manufacturer_data) < 8 or not manufacturer_data.startswith(ADV_TYPE_PREFIX):
        raise ValueError("not a feeder advertisement")
    mac_bytes = manufacturer_data[-6:]
    mac = ":".join(f"{b:02X}" for b in mac_bytes)
    model = None
    if local_name and "-" in local_name:
        model = local_name.split("-", 1)[0]
    return {
        "product_type": manufacturer_data[:2],
        "mac": mac,
        "model": model,
    }


# ---------------------------------------------------------------------------
# Message framing
# ---------------------------------------------------------------------------
def encode_request(method: str, req_id: int, params=None) -> bytes:
    """Encode a request as the exact JSON the vendor app writes (compact, no spaces).

    ``params`` is embedded as a raw JSON value; pass ``None`` to omit the field.
    """
    if params is None:
        body = f'{{"m":"{method}","i":{req_id}}}'
    else:
        import json as _json

        body = f'{{"m":"{method}","i":{req_id},"p":{_json.dumps(params, separators=(",", ":"))}}}'
    return body.encode("utf-8")


def parse_response(data: bytes) -> dict:
    """Parse a notification payload into a normalized response dict.

    Returns one of:
      ``{"ok": True, "i": <id>, "r": <result>}``
      ``{"ok": False, "i": <id>, "error": ASF02Error}``

    Raises ``ValueError`` for non-JSON or structurally invalid payloads.
    """
    import json as _json

    try:
        msg = _json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"unparseable response: {data!r}") from e
    if not isinstance(msg, dict) or "i" not in msg:
        raise ValueError(f"malformed response (missing id): {data!r}")
    if "r" in msg:
        return {"ok": True, "i": msg["i"], "r": msg["r"]}
    if "e" in msg:
        e = msg["e"]
        raise ASF02Error(int(e.get("c", 0)), str(e.get("m", "unknown")))
    raise ValueError(f"response has neither r nor e: {data!r}")