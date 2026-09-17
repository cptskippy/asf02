"""Client machinery tests against a fake BLE transport.

Verifies the behaviors that bit us live:
- pending future registered BEFORE the write (fast-reply race)
- device errors map to ASF02Error / ASF02LockedError
- silent_ok turns missing responses into None (feeder.log)
- request id increments and matches
"""
import asyncio

import pytest

from asf02.client import ASF02Client, ASF02LockedError, ASF02ProtocolError
from asf02.protocol import ASF02Error


class FakeChar:
    def __init__(self, uuid):
        self.uuid = uuid


class FakeBleakClient:
    """Stands in for BleakClient; scripted responses keyed by method."""

    def __init__(self, script: dict | None = None):
        self.script = script or {}
        self.writes: list[tuple[str, bytes]] = []
        self.connected = False
        self.notifying = False
        self._notify_cb = None

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def start_notify(self, char_uuid, cb):
        self.notifying = True
        self._notify_cb = cb

    async def write_gatt_char(self, char_uuid, data, response=False):
        self.writes.append((char_uuid, bytes(data)))
        import json as _json

        msg = _json.loads(data.decode())
        method = msg["m"]
        rid = msg["i"]
        if method in self.script:
            resp = self.script[method]
            if resp is None:  # device silence
                return
            payload = _json.dumps(resp, separators=(",", ":")).encode()
            # Fire the notify SYNCHRONOUSLY inside the write — the fast-reply
            # case that exposed the registration race.
            self._notify_cb(FakeChar(char_uuid), payload)


@pytest.fixture
def client_with_fake(monkeypatch):
    from asf02 import client as client_mod

    created = {}

    class Factory:
        def __init__(self, address, timeout=20):
            self._fake = FakeBleakClient(created.get("script"))
            created["fake"] = self._fake
            self.address = address

        def __getattr__(self, name):
            return getattr(self._fake, name)

    monkeypatch.setattr(client_mod, "BleakClient", Factory)

    async def make(script=None, **kw):
        if script is not None:
            created["script"] = script
        c = ASF02Client("AA:BB:CC:DD:EE:FF", "tok", post_connect_delay=0.0, **kw)
        await c.connect()
        created["client"] = c
        return c, created

    return make


async def test_info_fast_reply_no_race(client_with_fake):
    # info responds instantly during the write (the race case)
    c, created = await client_with_fake({"info": {"i": 1, "r": {"f": "1.19.12", "z": -25200}}})
    info = await c.info()
    assert info["f"] == "1.19.12"
    await c.disconnect()


async def test_unlock_and_request_ids_increment(client_with_fake):
    c, created = await client_with_fake({
        "unlock": {"i": 1, "r": {"l": 1, "t": 1719183104, "z": -25200, "d": "0000", "r": 2, "k": "x"}},
        "feeder.status": {"i": 2, "r": {"s": 0, "n": 5, "b": 4500, "u": 10, "t": 5, "d": 0}},
    })
    await c.unlock()
    st = await c.status()
    assert st["n"] == 5
    # second request must use a fresh id
    assert created["fake"].writes[-1][1] == b'{"m":"feeder.status","i":2}'
    await c.disconnect()


async def test_locked_error_wrapping(client_with_fake):
    c, _ = await client_with_fake({"feeder.status": {"i": 1, "e": {"c": -31400, "m": "Device locked"}}})
    with pytest.raises(ASF02LockedError) as ei:
        await c.status()
    assert ei.value.code == -31400
    await c.disconnect()


async def test_protocol_error_code_passthrough(client_with_fake):
    c, _ = await client_with_fake(
        {"time.calibration": {"i": 1, "e": {"c": -32602, "m": "Invalid params"}}})
    with pytest.raises(ASF02Error) as ei:
        await c.sync_time(-25200)
    assert ei.value.code == -32602
    await c.disconnect()


async def test_silent_ok_returns_none(client_with_fake):
    c, _ = await client_with_fake({})  # feeder.log: no scripted reply = silence
    log = await c.get_log()
    assert log is None
    await c.disconnect()


async def test_get_log_parses_rows(client_with_fake):
    c, _ = await client_with_fake(
        {"feeder.log": {"i": 1, "r": {"d": [["1757", "0", "1"], ["0", "x", "y"]]}}})
    rows = await c.get_log()
    assert rows == [["1757", "0", "1"], ["0", "x", "y"]]
    await c.disconnect()


async def test_feed_validates_portions(client_with_fake):
    c, _ = await client_with_fake({})
    with pytest.raises(ValueError):
        await c.feed(0)
    with pytest.raises(ValueError):
        await c.feed(-1)
    with pytest.raises(ValueError):
        await c.feed(100)   # vendor picker caps at 99
    await c.disconnect()


async def test_request_without_connect_raises(client_with_fake):
    c = ASF02Client("AA:BB:CC:DD:EE:FF", "tok")
    with pytest.raises(ASF02ProtocolError):
        await c.info()
