"""Pretend to be the Switch plugin so the PC side can be tested without hardware.

    python -m bridge.fake_switch                 # heartbeats + a fake 60 fps match
    python -m bridge.fake_switch --no-match      # heartbeats only
    python -m bridge.fake_switch --drop 0.02     # throw away 2% of packets

Sends the same packets as plugin/src/net.rs: a heartbeat every 500 ms and,
during the fake match, one state packet per frame with two R.O.B.s drifting
around and taking damage.
"""
from __future__ import annotations

import argparse
import math
import random
import socket
import time

from .protocol import Fighter, Frame
from .wire import pack_heartbeat, pack_state

ROBOT = 0x2D
FRAME_TIME = 1 / 60


def fake_fighter(t: float, side: int) -> Fighter:
    return Fighter(
        fighter_kind=ROBOT,
        pos_x=side * 40 + 30 * math.sin(t + side),
        pos_y=max(0.0, 25 * math.sin(2 * t)),
        vel_x=0.0, vel_y=0.0,
        damage=(t * 3 * (1.5 if side > 0 else 1)) % 150,
        shield_hp=0.0,
        stocks=3,
        status_kind=0,
        motion_kind=0,
        anim_frame=(t * 60) % 30,
        facing=-side,
        jumps_left=0,
        flags=1 if math.sin(2 * t) > 0 else 0,
    )


def run(host: str, port: int, drop: float, version: str, match: bool, seconds: float | None) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    start = time.monotonic()
    next_hb = start
    seq = frame = sent = dropped = 0
    print(f"[fake] sending to {host}:{port}, {'with' if match else 'without'} a match, dropping {drop:.0%}")

    def send(packet: bytes) -> None:
        nonlocal sent, dropped, seq
        if random.random() >= drop:
            sock.sendto(packet, (host, port))
            sent += 1
        else:
            dropped += 1
        seq += 1

    while seconds is None or time.monotonic() - start < seconds:
        now = time.monotonic()
        if match:
            t = now - start
            send(pack_state(seq, Frame(frame, 0, fake_fighter(t, -1), fake_fighter(t, 1))))
            frame += 1
        if now >= next_hb:
            next_hb = now + 0.5
            send(pack_heartbeat(seq, int((now - start) * 1000), version, True, 2 * frame, frame))
        time.sleep(FRAME_TIME if match else 0.5)
    print(f"[fake] done: sent {sent}, dropped {dropped}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4243)
    ap.add_argument("--drop", type=float, default=0.0)
    ap.add_argument("--version", default="13.0.4")
    ap.add_argument("--no-match", action="store_true", help="heartbeats only")
    ap.add_argument("--seconds", type=float, default=None)
    args = ap.parse_args()
    run(args.host, args.port, args.drop, args.version, not args.no_match, args.seconds)


if __name__ == "__main__":
    main()
