"""Pretend to be the Switch plugin so the PC side can be tested without hardware.

    python -m bridge.fake_switch                 # heartbeats + a fake 60 fps match
    python -m bridge.fake_switch --no-match      # heartbeats only
    python -m bridge.fake_switch --drop 0.02     # throw away 2% of packets

Sends the same packets as plugin/src/net.rs: a heartbeat every 500 ms and,
during the fake match, one state packet per frame with two R.O.B.s on
Battlefield drifting around, hopping, shielding and taking damage.

`--version 13.0.3` pretends the Switch runs the wrong game build: like the
real plugin, it then sends heartbeats only.
"""
from __future__ import annotations

import argparse
import math
import random
import socket
import time

from .protocol import Fighter, Frame
from .stages import BATTLEFIELD
from .wire import pack_heartbeat, pack_state

ROBOT = 0x2D
FRAME_TIME = 1 / 60
EXPECTED_GAME_VERSION = "13.0.4"  # plugin/src/version.rs


SHIELD_MAX = 50.0  # every fighter's full shield in Ultimate
JUMPS_MAX = 2  # R.O.B.: one ground jump + one double jump
STATUS_JUMP, STATUS_JUMP_AERIAL, STATUS_GUARD, STATUS_DAMAGE_AIR = 0xB, 0xC, 0x1C, 0x48
FLAG_AIRBORNE, FLAG_HITSTUN = 1, 2


def fake_fighter(t: float, side: int) -> Fighter:
    """Two R.O.B.s on Battlefield. Each hops on a 2 second loop (double
    jumping halfway up), shields between hops, and the P2 side gets
    knocked into hitstun once every 5 seconds."""
    hop = 2 * t
    y = max(0.0, 25 * math.sin(hop))
    airborne = math.sin(hop) > 0
    # position and its time derivative, per frame like the game reports it
    x = side * 40 + 30 * math.sin(t + side)
    vx = 30 * math.cos(t + side) / 60
    vy = (50 * math.cos(hop) / 60) if airborne else 0.0
    phase = hop % (2 * math.pi)
    jumps_left = JUMPS_MAX - (0 if not airborne else (1 if phase < math.pi / 2 else 2))
    hit = side > 0 and (t % 5) < 0.5
    shielding = not airborne and not hit
    # shield drains while held and tops back up while in the air
    shield = SHIELD_MAX - (8 * ((t * 2) % 1) if shielding else 0.0)
    if hit:
        status, flags = STATUS_DAMAGE_AIR, FLAG_AIRBORNE | FLAG_HITSTUN
    elif airborne:
        status, flags = (STATUS_JUMP_AERIAL if jumps_left == 0 else STATUS_JUMP), FLAG_AIRBORNE
    else:
        status, flags = STATUS_GUARD, 0
    return Fighter(
        fighter_kind=ROBOT,
        pos_x=x,
        pos_y=y,
        vel_x=vx, vel_y=vy,
        damage=(t * 3 * (1.5 if side > 0 else 1)) % 150,
        shield_hp=shield,
        stocks=3,
        status_kind=status,
        motion_kind=0,
        anim_frame=(t * 60) % 30,
        facing=-side,
        jumps_left=jumps_left,
        flags=flags,
    )


def run(host: str, port: int, drop: float, version: str, match: bool, seconds: float | None) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    start = time.monotonic()
    next_hb = start
    seq = frame = sent = dropped = 0
    if match and version != EXPECTED_GAME_VERSION:
        # same rule as plugin/src/version.rs: wrong build, no state
        print(f"[fake] version {version} != {EXPECTED_GAME_VERSION}, heartbeats only")
        match = False
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
            send(pack_state(seq, Frame(frame, BATTLEFIELD, fake_fighter(t, -1), fake_fighter(t, 1))))
            frame += 1
        if now >= next_hb:
            next_hb = now + 0.5
            send(pack_heartbeat(seq, int((now - start) * 1000), version, True, 2 * frame, frame))
        time.sleep(FRAME_TIME if match else 0.5)
    sock.close()
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
