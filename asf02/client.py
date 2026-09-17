"""ASF02 device client: connect, unlock, and the JSON-RPC command set.

Built on `bleak`. All command methods are transient-connection friendly:
connect, do the work, disconnect — the feeders run autonomously between
visits (mirrors the vendor app's usage pattern).

Key protocol behaviors (live-verified against a real unit):
- The device is locked across every (re)connect; only ``info`` works while
  locked. ``connect_unlocked()`` performs the unlock dance.
- ``time.calibration`` takes epoch SECONDS and a tz offset in SECONDS
  (e.g. -25200 for PDT, DST included).
- Plan writes must use the full 10-slot array form; the write *response* is
  the authoritative confirmation (reads are unreliable on some firmware).
- ``feeder.plan`` reads require a ``p`` field (e.g. ``""``); omitting it
  yields device silence.
- ``feeder.log`` may return no notification at all — callers get ``None``.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any, Iterable, Sequence

from bleak import BleakClient

from .protocol import (
    ASF02Error,
    CCCD_UUID,
    NOTIFY_CHAR_UUID,
    SERVICE_UUID,
    WRITE_CHAR_UUID,
    decode_plan_slot,
    encode_request,
    generate_token,
    parse_response,
    unlock_code_for,
)


class ASF02ProtocolError(ASF02Error):
    """Local protocol violation (bad frame shape, missing service, ...)."""


class ASF02LockedError(ASF02Error):
    """The device refused a command because it is locked (code -31400)."""


def _wrap_errors(e: Exception) -> Exception:
    if isinstance(e, ASF02Error):
        if e.code == -31400:
            return ASF02LockedError(e.code, e.message)
        return e
    return e


class ASF02Client:
    """A single ASF02 feeder.

    Example:
        async with ASF02Client("<DEVICE_MAC>", token="<BIND_TOKEN>") as f:
            info = await f.info()
            await f.sync_time(tz_offset_seconds=-25200)
            schedule = await f.get_schedule()
            await f.set_schedule([(15, 30, 1), (16, 0, 1)])
            await f.feed(1)
    """

    def __init__(self, address: str, token: str, *, response_timeout: float = 10.0,
                 post_connect_delay: float = 1.0):
        self.address = address
        self.token = token
        self.response_timeout = response_timeout
        self.post_connect_delay = post_connect_delay
        self._client: BleakClient | None = None
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._unlocked = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    async def __aenter__(self) -> "ASF02Client":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        """Connect and enable notifications. Device is locked until unlock()."""
        if self._client is not None:
            return
        client = BleakClient(self.address, timeout=20)
        await client.connect()
        await client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
        # Give the controller a moment before the first command (matches the
        # vendor app's connect-then-poll behavior and avoids early drops).
        if self.post_connect_delay:
            await asyncio.sleep(self.post_connect_delay)
        self._client = client

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        self._unlocked = False
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()

    async def unlock(self) -> dict:
        """Unlock the device using the configured token. Returns the device's
        unlock result dict (contains device time ``t``, tz ``z``, ``l``, ``k``)."""
        if self._client is None:
            raise ASF02ProtocolError(0, "not connected")
        result = await self._request("unlock", {"s": unlock_code_for(self.token)})
        self._unlocked = True
        return result

    async def connect_unlocked(self) -> dict:
        """connect() + unlock() in one step (the usual entry point)."""
        await self.connect()
        return await self.unlock()

    @property
    def connected(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    async def info(self) -> dict:
        """Device info (works even while locked): firmware ``f``, hw ``h``,
        model ``m``, serial ``s``, timezone offset ``z`` (seconds)."""
        return await self._request("info")

    async def sync_time(self, tz_offset_seconds: int, *, epoch: int | None = None) -> dict:
        """Calibrate the device clock.

        ``tz_offset_seconds`` = total UTC offset **in seconds, including DST**
        (e.g. -25200 for PDT, -28800 for PST — match the vendor app, which
        sends ``(rawOffset + dstSavings) / 1000``). The device clock can drift
        seconds a month, so sync as needed.
        """
        t = int(epoch if epoch is not None else time.time())
        return await self._request("time.calibration", {"t": t, "z": tz_offset_seconds})

    async def status(self) -> dict:
        """Live status: ``s`` state, ``n``/``t`` total feed counter, ``b``
        battery (units unconfirmed; <=410 = low per vendor app), ``u`` uptime-ish
        counter, ``d`` (unknown, 0)."""
        return await self._request("feeder.status")

    async def feed(self, portions: int) -> Any:
        """Dispense ``portions`` immediately (1-99, matching the vendor app's
        picker). Returns ``True`` as a near-instant ACK — the dispense runs
        ASYNCHRONOUSLY afterwards (s=1 while the motor runs; a single
        feed(10) was observed to take ~50s). The device drops the GATT
        connection during the feed, so do NOT poll status() while it runs:
        disconnect, wait, reconnect, then read. The status ``n``/``t``
        counters increment by ``portions`` (live-verified: feed(10) moved n
        by +10, confirmed across three independent deltas)."""
        if not isinstance(portions, int) or not 1 <= portions <= 99:
            raise ValueError("portions must be an integer 1..99")
        return await self._request("feeder.feed", {"n": portions})

    async def stop(self) -> Any:
        """Stop an in-progress feed (safe no-op when idle)."""
        return await self._request("feeder.stop")

    async def get_schedule(self) -> list:
        """Read the schedule as a list of :class:`~asf02.protocol.PlanEntry`.

        Returns the non-empty slots in device slot order. Returns an empty
        list when the device stays silent (observed with an empty schedule on
        some firmware) or reports only empty slots.
        """
        r = await self._request("feeder.plan", params_raw="", silent_ok=True)
        slots = r.get("p") if isinstance(r, dict) else None
        if not isinstance(slots, list):
            return []
        entries = [decode_plan_slot(s) for s in slots if isinstance(s, str)]
        return [e for e in entries if e is not None]

    async def set_schedule(self, entries: Iterable, *, clear: bool = False) -> list:
        """Replace the entire schedule (device stores a fixed 10-slot array).

        ``entries`` is an iterable of ``(hour, minute, portion)`` or
        ``(hour, minute, portion, enabled)`` tuples, max 10. Use ``clear=True``
        (or an empty iterable) to wipe the schedule.

        Returns the full 10-slot array the device confirmed as stored —
        this response is the authoritative confirmation.
        """
        entries = list(entries)
        if clear:
            payload = ["0000000000"] + [""] * 9
        elif not entries:
            payload = ["0000000000"] + [""] * 9
        else:
            if len(entries) > 10:
                raise ValueError(f"too many schedule entries: {len(entries)} (max 10)")
            from .protocol import encode_plan_entry

            payload = [encode_plan_entry(h, m, w, on) for (h, m, w, *rest) in entries
                       for on in [bool(rest[0]) if rest else True]]
            payload += [""] * (10 - len(payload))
        r = await self._request("feeder.plan", params_raw=json.dumps(payload, separators=(",", ":")))
        slots = r.get("p") if isinstance(r, dict) else None
        return list(slots) if isinstance(slots, list) else []

    async def get_log(self) -> list | None:
        """Read feed history. Returns the device's ``d`` row list, or ``None``
        when the device stays silent (common on some firmware/empty history)."""
        r = await self._request("feeder.log", silent_ok=True)
        if isinstance(r, dict) and "d" in r:
            return r["d"]
        return None

    # ------------------------------------------------------------------
    # Pairing
    # ------------------------------------------------------------------
    @staticmethod
    def new_token() -> str:
        """Generate a fresh bind token (10 chars, vendor charset)."""
        return generate_token()

    async def bind(self, token: str | None = None, *, home_id: str = "0",
                   tz_offset_seconds: int = 0) -> dict:
        """(Re)bind the device to ``token`` — overwrites any previous token.
        ``tz_offset_seconds`` = total UTC offset in seconds incl. DST (same
        convention as :meth:`sync_time`). After this call the old token no
        longer unlocks the device."""
        tok = token if token is not None else self.token
        return await self._request(
            "bind", {"h": str(home_id), "d": "0000", "t": tok, "z": tz_offset_seconds}
        )

    # ------------------------------------------------------------------
    # Core request/response machinery
    # ------------------------------------------------------------------
    async def _request(self, method: str, params: dict | None = None,
                       *, params_raw: str | None = None,
                       silent_ok: bool = False) -> Any:
        """Send one command and await its response.

        - ``params``: normal dict params (serialized compactly).
        - ``params_raw``: raw JSON fragment for ``p`` (used for the plan
          read/write quirks where the param shape matters exactly).
        - ``silent_ok``: if True, a missing response returns ``None`` instead
          of raising (used for feeder.log, which may stay silent).

        Raises :class:`ASF02LockedError` / :class:`ASF02Error` on device
        errors, ``asyncio.TimeoutError`
        on silence (unless ``silent_ok``).
        """
        client = self._client
        if client is None:
            raise ASF02ProtocolError(0, "not connected")
        self._req_id += 1
        req_id = self._req_id
        if params_raw is not None:
            payload = f'{{"m":"{method}","i":{req_id},"p":{params_raw}}}'.encode("utf-8")
        else:
            payload = encode_request(method, req_id, params)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        try:
            # Register the future BEFORE writing: fast device replies (e.g.
            # info) can arrive immediately and must not be dropped.
            await client.write_gatt_char(WRITE_CHAR_UUID, payload, response=True)
            try:
                return await asyncio.wait_for(fut, self.response_timeout)
            except asyncio.TimeoutError:
                if silent_ok:
                    return None
                raise
        except ASF02Error as e:
            raise _wrap_errors(e) from None

    def _on_notify(self, _char: Any, data: bytearray) -> None:
        raw = bytes(data)
        try:
            resp = parse_response(raw)
        except ASF02Error as e:
            fut = self._pending.pop(self._guess_id(raw), None)
            if fut is not None and not fut.done():
                fut.set_exception(_wrap_errors(e))
            return
        except ValueError:
            return  # non-JSON noise; ignore
        fut = self._pending.pop(resp["i"], None)
        if fut is None or fut.done():
            return
        if resp["ok"]:
            fut.set_result(resp["r"])
        else:
            fut.set_exception(resp["error"])

    @staticmethod
    def _guess_id(raw: bytes) -> int:
        """Best-effort id extraction from a malformed-error frame."""
        with contextlib.suppress(Exception):
            return int(json.loads(raw.decode("utf-8"))["i"])
        return -1
