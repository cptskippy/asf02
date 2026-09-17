#!/usr/bin/env python3
"""Demo CLI for the asf02 library — run next to a feeder (e.g. on a Pi).

    python3 examples/cli.py --address <DEVICE_MAC> --token <BIND_TOKEN> info
    python3 examples/cli.py --address ... --token ... status
    python3 examples/cli.py --address ... --token ... schedule
    python3 examples/cli.py --address ... --token ... set-schedule 15:30/1 16:00/1
    python3 examples/cli.py --address ... --token ... feed 1
    python3 examples/cli.py scan
"""
import argparse
import asyncio
import sys

sys.path.insert(0, "..")  # allow running without install

from asf02 import ASF02Client, discover_feeders  # noqa: E402


def parse_entry(spec: str):
    """'15:30/1' -> (15, 30, 1); '16:00' -> (16, 0, 1)"""
    time_part, _, portion_part = spec.partition("/")
    h, m = time_part.split(":")
    return int(h), int(m), int(portion_part) if portion_part else 1


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["scan", "info", "status", "schedule", "set-schedule", "feed", "log", "bind-token"])
    ap.add_argument("--address")
    ap.add_argument("--token")
    ap.add_argument("args", nargs="*", help="set-schedule entries (15:30/1) or feed count")
    ap.add_argument("--tz-seconds", type=int, default=-25200,
                    help="UTC offset in seconds incl. DST (PDT=-25200, PST=-28800)")
    args = ap.parse_args()

    if args.action == "scan":
        found = await discover_feeders(timeout=10)
        if not found:
            print("no feeders found")
        for f in found:
            print(f"  {f.address}  {f.name:18s}  model={f.model}  rssi={f.rssi}  mfg={f.raw_manufacturer_data}")
        return

    if not (args.address and args.token):
        ap.error("--address and --token are required for device actions")

    async with ASF02Client(args.address, args.token) as f:
        if args.action == "info":
            await f.unlock()
            print(await f.info())
        elif args.action == "status":
            await f.unlock()
            print(await f.status())
        elif args.action == "schedule":
            await f.unlock()
            await f.sync_time(args.tz_seconds)
            for e in await f.get_schedule():
                print(f"  {e.hour:02d}:{e.minute:02d}  portion={e.portion}  enabled={e.enabled}")
        elif args.action == "set-schedule":
            await f.unlock()
            await f.sync_time(args.tz_seconds)
            entries = [parse_entry(s) for s in args.args]
            stored = await f.set_schedule(entries)
            print("device stored slots:", stored)
        elif args.action == "feed":
            await f.unlock()
            n = int(args.args[0]) if args.args else 1
            print("feed result:", await f.feed(n))
            print("status after:", await f.status())
        elif args.action == "log":
            await f.unlock()
            print("log:", await f.get_log())
        elif args.action == "bind-token":
            print("WARNING: this overwrites the device's stored token")
            print("bind result:", await f.bind(args.token, tz_offset_seconds=args.tz_seconds))


if __name__ == "__main__":
    asyncio.run(main())
