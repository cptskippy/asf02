"""Discovery: scan for ASF02 feeders via bleak.

Uses the feeder's advertising record (name ``ASF02-<last6MAC>`` +
manufacturer data ``0203 ... <MAC>``). Company id 0xFFFF, type prefix 0203.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from bleak import BleakScanner

from .protocol import ADV_COMPANY_ID, decode_advertisement


@dataclass
class DiscoveredFeeder:
    address: str
    name: str
    model: str | None
    mac: str
    rssi: int
    raw_manufacturer_data: str  # hex


async def discover_feeders(timeout: float = 10.0, *, address_filter: str | None = None) -> list[DiscoveredFeeder]:
    """Scan up to ``timeout`` seconds and return all feeders seen.

    ``address_filter`` (a MAC like ``"AA:BB:CC:DD:EE:FF"``) limits results to
    one device (useful when several feeders are nearby).
    """
    found: dict[str, DiscoveredFeeder] = {}

    def on_adv(device, adv) -> None:
        if address_filter and device.address != address_filter:
            return
        mfg = None
        for cid, data in (adv.manufacturer_data or {}).items():
            if cid == ADV_COMPANY_ID:
                mfg = data
                break
        if mfg is None:
            return
        try:
            decoded = decode_advertisement(mfg, local_name=adv.local_name or device.name)
        except ValueError:
            return
        rec = found.setdefault(device.address, DiscoveredFeeder(
            address=device.address,
            name=device.name or "",
            model=decoded.get("model"),
            mac=decoded["mac"],
            rssi=adv.rssi,
            raw_manufacturer_data=mfg.hex(),
        ))
        rec.rssi = max(rec.rssi, adv.rssi)
        if adv.local_name:
            rec.name = adv.local_name

    scanner = BleakScanner(on_adv)
    await scanner.start()
    try:
        await asyncio.sleep(timeout)
    finally:
        await scanner.stop()
    return sorted(found.values(), key=lambda f: f.rssi, reverse=True)
