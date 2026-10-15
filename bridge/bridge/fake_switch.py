"""Pretend to be the Switch plugin so the PC side can be tested without hardware.

    python -m bridge.fake_switch --host 127.0.0.1 --port 4243 --drop 0.02

Sends the same heartbeat packets as plugin/src/net.rs. `--drop` throws away
that fraction of packets on purpose so you can check the listener counts gaps.
"""
from __future__ import annotations

import argparse
import random
import socket
import time

from .wire import pack_heartbeat


def run(host: str, port: int, every: float, drop: float, version: str, count: int | None) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    start = time.monotonic()
    seq = 0
    sent = dropped = 0
    print(f"[fake] sending heartbeats to {host}:{port}, dropping {drop:.0%}")
    while count is None or seq < count:
        if random.random() >= drop:
            uptime = int((time.monotonic() - start) * 1000)
            sock.sendto(pack_heartbeat(seq, uptime, version), (host, port))
            sent += 1
        else:
            dropped += 1
        seq += 1
        time.sleep(every)
    print(f"[fake] done: sent {sent}, dropped {dropped}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4243)
    ap.add_argument("--every", type=float, default=0.5, help="seconds between packets")
    ap.add_argument("--drop", type=float, default=0.0)
    ap.add_argument("--version", default="13.0.4")
    ap.add_argument("--count", type=int, default=None)
    args = ap.parse_args()
    run(args.host, args.port, args.every, args.drop, args.version, args.count)


if __name__ == "__main__":
    main()
