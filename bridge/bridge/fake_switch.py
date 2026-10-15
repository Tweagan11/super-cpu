"""Pretend to be the Switch plugin so the PC side can be tested without hardware.

    python -m bridge.fake_switch                 # heartbeats + a fake 60 fps match
    python -m bridge.fake_switch --no-match      # heartbeats only
    python -m bridge.fake_switch --drop 0.02     # throw away 2% of packets
    python -m bridge.fake_switch --matches 3     # 3 short matches with stock losses, a winner, and gaps

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

from dataclasses import replace

from .protocol import Fighter, Frame
from .stages import STAGE_ID_BATTLEFIELD, STAGE_ID_UNKNOWN
from .statuses import STATUS_NAMES
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
            send(pack_state(seq, Frame(frame, STAGE_ID_BATTLEFIELD, fake_fighter(t, -1), fake_fighter(t, 1))))
            frame += 1
        if now >= next_hb:
            next_hb = now + 0.5
            send(pack_heartbeat(seq, int((now - start) * 1000), version, True, 2 * frame, frame))
        time.sleep(FRAME_TIME if match else 0.5)
    sock.close()
    print(f"[fake] done: sent {sent}, dropped {dropped}")


DEAD = next(k for k, v in STATUS_NAMES.items() if v == "DEAD")
ATTACK_S3 = next(k for k, v in STATUS_NAMES.items() if v == "ATTACK_S3")


def scripted_match(
    n_frames: int, winner: int, winner_losses: int, stocks: int = 3,
    stage_id: int = STAGE_ID_UNKNOWN,
) -> list[Frame]:
    """Frames for one fake match. The loser loses all `stocks`, the winner
    loses `winner_losses`, spread over the match. Damage climbs and resets on
    each stock loss, and a fighter sits in DEAD for a few frames after one.
    """
    loser = 3 - winner
    losses = {loser: stocks, winner: winner_losses}
    # Frame indexes where each port loses a stock, evenly spaced, last one near the end.
    kill_at = {
        port: [round(n_frames * (k + 1) / (n + 0.25)) for k in range(n)] if n else []
        for port, n in losses.items()
    }
    kill_at[loser][-1] = n_frames - 10
    frames = []
    for i in range(n_frames):
        t = i / 60
        fs = []
        for port, side in ((1, -1), (2, 1)):
            f = fake_fighter(t, side)
            done = sum(1 for k in kill_at[port] if k <= i)
            since = min((i - k for k in kill_at[port] if k <= i), default=None)
            last_kill = max((k for k in kill_at[port] if k <= i), default=0)
            damage = (i - last_kill) * (0.25 if port == winner else 0.4)
            status = DEAD if since is not None and since < 5 else (ATTACK_S3 if (i // 40) % 3 == 0 else 0)
            fs.append(replace(f, stocks=stocks - done, damage=damage, status_kind=status))
        frames.append(Frame(i, stage_id, fs[0], fs[1]))
    return frames


def run_matches(
    host: str, port: int, drop: float, version: str, n_matches: int,
    match_seconds: float, gap: float, winners: list[int], realtime: bool = True,
) -> None:
    """Play `n_matches` fake matches back to back with `gap` seconds of
    heartbeats-only between them, like the results screen and rematch."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    start = time.monotonic()
    seq = sent = dropped = 0
    next_hb = start
    rng = random.Random(1234)
    print(f"[fake] sending {n_matches} matches to {host}:{port}, {match_seconds}s each, {gap}s gaps")

    def send(packet: bytes) -> None:
        nonlocal sent, dropped, seq
        if rng.random() >= drop:
            sock.sendto(packet, (host, port))
            sent += 1
        else:
            dropped += 1
        seq += 1

    def heartbeat_if_due(frames_done: int) -> None:
        nonlocal next_hb
        now = time.monotonic()
        if now >= next_hb:
            next_hb = now + 0.5
            send(pack_heartbeat(seq, int((now - start) * 1000), version, True, 2 * frames_done, frames_done))

    def idle(seconds: float, frames_done: int) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            heartbeat_if_due(frames_done)
            time.sleep(0.05)

    total = 0
    idle(0.6, 0)  # make sure a heartbeat lands before the first match
    for m in range(n_matches):
        winner = winners[m % len(winners)]
        match = scripted_match(int(match_seconds * 60), winner, winner_losses=m % 3)
        print(f"[fake] match {m + 1}/{n_matches}: P{winner} wins")
        t0 = time.monotonic()
        for i, f in enumerate(match):
            send(pack_state(seq, f))
            total += 1
            heartbeat_if_due(total)
            if realtime:
                delay = t0 + (i + 1) * FRAME_TIME - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        idle(gap, total)
    print(f"[fake] done: sent {sent}, dropped {dropped}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4243)
    ap.add_argument("--drop", type=float, default=0.0)
    ap.add_argument("--version", default="13.0.4")
    ap.add_argument("--no-match", action="store_true", help="heartbeats only")
    ap.add_argument("--seconds", type=float, default=None)
    ap.add_argument("--matches", type=int, default=None, help="play N short scripted matches, then stop")
    ap.add_argument("--match-seconds", type=float, default=6.0)
    ap.add_argument("--gap", type=float, default=2.5, help="seconds between matches (must beat the 1.5 s end timeout)")
    ap.add_argument("--winners", default="2,2,1", help="winning port per match, cycled, e.g. 2,2,1")
    args = ap.parse_args()
    if args.matches:
        winners = [int(w) for w in args.winners.split(",")]
        run_matches(args.host, args.port, args.drop, args.version, args.matches,
                    args.match_seconds, args.gap, winners)
        return
    run(args.host, args.port, args.drop, args.version, not args.no_match, args.seconds)


if __name__ == "__main__":
    main()
