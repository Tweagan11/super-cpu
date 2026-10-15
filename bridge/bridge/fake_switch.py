"""Pretend to be the Switch plugin so the PC side can be tested without hardware.

    python -m bridge.fake_switch                 # heartbeats + a fake 60 fps match
    python -m bridge.fake_switch --no-match      # heartbeats only
    python -m bridge.fake_switch --drop 0.02     # throw away 2% of packets it sends
    python -m bridge.fake_switch --latency-ms 20 # inputs take 20 ms to "arrive"

Sends the same packets as plugin/src/net.rs: a heartbeat plus a control
status every 500 ms and, during the fake match, one state packet per frame
with two R.O.B.s drifting around and taking damage.

It also takes inputs like the plugin does (SSB-6): input packets that come
back to its socket drive the bot's port (`--bot-port`, default 2) with the
same rules as plugin/src/input.rs (late input dropped, repeat up to 6
frames, then neutral, 2 s without input hands the port back to its "AI"
drift), and every applied input is acked (kind 4).

Timing matches the real plugin: the input for frame N is picked at the start
of frame N, and state for frame N goes out at the start of frame N + 1, so
the fastest possible round trip is 2 frames.
"""
from __future__ import annotations

import argparse
import heapq
import math
import random
import socket
import time
from dataclasses import dataclass, replace

from .protocol import Fighter, Frame, Inputs
from .wire import (
    FLAG_HOOK_INSTALLED,
    FLAG_RECEIVING,
    FLAG_SESSION_ACTIVE,
    KIND_INPUT,
    BadPacket,
    ControlStatus,
    Header,
    pack_ack,
    pack_control_status,
    pack_heartbeat,
    pack_state,
    unpack_header,
    unpack_input,
)

ROBOT = 0x2D
FRAME_TIME = 1 / 60

# Same numbers as plugin/src/input.rs
REPEAT_FRAMES = 6
SESSION_TIMEOUT = 2.0
RING = 32
RESYNC_AFTER = 30

WALK_SPEED = 1.2  # units per frame at full tilt


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


@dataclass
class Counters:
    inputs_rx: int = 0
    applied: int = 0
    late: int = 0
    rejected: int = 0
    repeats: int = 0
    neutral: int = 0


class FakeInputSide:
    """Python copy of plugin/src/input.rs (InputReceiver + Applier), close
    enough to test the bridge against. `now` is seconds on any clock."""

    def __init__(self) -> None:
        self.pending: dict[int, tuple[int, Inputs, int]] = {}  # apply_at -> (for_frame, inputs, ready)
        self.last_rx: float | None = None
        self.last_seq: int | None = None
        self.old_streak = 0
        self.last_for: int | None = None
        self.last_target: int | None = None
        self.last_out = Inputs()
        self.last_fresh: Inputs | None = None
        self.since_fresh = 0
        self.c = Counters()

    def session_active(self, now: float) -> bool:
        return self.last_rx is not None and now - self.last_rx < SESSION_TIMEOUT

    def receive(self, buf: bytes, now: float, next_target: int) -> str:
        try:
            p = unpack_input(buf)
        except BadPacket:
            self.c.rejected += 1
            return "bad"
        if not self.session_active(now):
            self.last_seq = self.last_for = None
            self.old_streak = 0
            self.pending.clear()
        if self.last_seq is not None:
            step = (p.header.seq - self.last_seq) & 0xFFFFFFFF
            if step == 0 or step > 0x7FFFFFFF:
                self.old_streak += 1
                if self.old_streak < RESYNC_AFTER:
                    self.c.rejected += 1
                    return "old"
                self.pending.clear()
                self.last_for = None
        self.old_streak = 0
        self.last_seq = p.header.seq
        self.last_rx = now
        self.c.inputs_rx += 1
        if self.last_for is not None and self.last_for > p.for_frame + RING:
            self.pending.clear()
        self.last_for = p.for_frame
        if p.apply_at < next_target:
            self.c.late += 1
            return "late"
        if p.apply_at - next_target >= RING:
            self.c.rejected += 1
            return "far"
        self.pending[p.apply_at] = (p.for_frame, p.inputs, next_target)
        return "queued"

    def step(self, target: int, now: float) -> tuple[Inputs | None, tuple[int, int, int] | None]:
        """Returns (inputs or None for hands off, ack or None)."""
        if not self.session_active(now):
            self.last_target = None
            self.last_fresh = None
            self.since_fresh = 0
            return None, None
        if self.last_target == target:
            return self.last_out, None
        if self.last_target is not None and target < self.last_target:
            self.last_fresh = None
            self.since_fresh = 0
        self.last_target = target
        for k in [k for k in self.pending if k < target]:
            del self.pending[k]
        ack = None
        hit = self.pending.pop(target, None)
        if hit is not None and hit[0] <= target:
            for_frame, out, ready = hit
            self.c.applied += 1
            ack = (for_frame, target, ready)
            self.last_fresh = out
            self.since_fresh = 0
        else:
            self.since_fresh += 1
            if self.last_fresh is not None and self.since_fresh <= REPEAT_FRAMES:
                self.c.repeats += 1
                out = self.last_fresh
            else:
                self.c.neutral += 1
                out = Inputs()
        self.last_out = out
        return out, ack


def run(host: str, port: int, drop: float, version: str, match: bool, seconds: float | None,
        bot_port: int = 2, latency_ms: float = 0.0, input_drop: float = 0.0, quiet: bool = False) -> FakeInputSide:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.setblocking(False)
    start = time.monotonic()
    next_hb = start
    seq = frame = sent = dropped = 0
    side = FakeInputSide()
    bot_x: float | None = None  # set while the PC is driving
    in_flight: list[tuple[float, int, bytes]] = []  # (deliver_at, n, packet)
    n_rx = 0
    pending_state: Frame | None = None  # finished frame, sent when the next one starts
    log = (lambda _m: None) if quiet else print
    log(f"[fake] sending to {host}:{port}, {'with' if match else 'without'} a match, dropping {drop:.0%}, "
        f"bot on port {bot_port}, input latency {latency_ms:.0f} ms")

    def send(packet: bytes) -> None:
        nonlocal sent, dropped, seq
        if random.random() >= drop:
            try:
                sock.sendto(packet, (host, port))
            except ConnectionResetError:
                pass
            sent += 1
        else:
            dropped += 1
        seq = (seq + 1) & 0xFFFFFFFF

    def pump(now: float) -> None:
        """Pull everything off the socket into the in-flight queue, then hand
        over whatever has 'arrived'."""
        nonlocal n_rx
        while True:
            try:
                buf, _ = sock.recvfrom(2048)
            except (BlockingIOError, ConnectionResetError):
                break
            if random.random() < input_drop:
                continue
            n_rx += 1
            heapq.heappush(in_flight, (now + latency_ms / 1000, n_rx, buf))
        while in_flight and in_flight[0][0] <= now:
            _, _, buf = heapq.heappop(in_flight)
            try:
                if unpack_header(buf).kind != KIND_INPUT:
                    continue
            except BadPacket:
                side.c.rejected += 1
                continue
            side.receive(buf, now, (side.last_target + 1) if side.last_target is not None else frame)

    def status() -> ControlStatus:
        flags = FLAG_HOOK_INSTALLED | FLAG_RECEIVING
        if side.session_active(time.monotonic()):
            flags |= FLAG_SESSION_ACTIVE
        c = side.c
        return ControlStatus(
            Header(2, 5, 0), bot_port, bot_port - 1, flags,
            fim_calls=2 * frame, idx_seen=0b11, inputs_rx=c.inputs_rx, applied=c.applied, late=c.late,
            rejected=c.rejected, repeats=c.repeats, neutral=c.neutral,
        )

    next_tick = start
    try:
        while seconds is None or time.monotonic() - start < seconds:
            now = time.monotonic()
            pump(now)
            if match and now >= next_tick:
                next_tick += FRAME_TIME
                if next_tick < now - 0.1:
                    next_tick = now  # fell way behind (debugger, sleep); don't burst
                t = now - start
                # 1. the game maps the bot's controller for this frame
                inputs, ack = side.step(frame, now)
                # 2. the fighters update; the first one flushes last frame's state
                if pending_state is not None:
                    send(pack_state(seq, pending_state))
                p1, p2 = fake_fighter(t, -1), fake_fighter(t, 1)
                bot = p2 if bot_port == 2 else p1
                if inputs is None:
                    bot_x = None  # back to the "AI"
                else:
                    if bot_x is None:
                        bot_x = bot.pos_x
                    bot_x += WALK_SPEED * inputs.stick_x / 127
                    bot_x = max(-150.0, min(150.0, bot_x))
                    bot = replace(bot, pos_x=bot_x, pos_y=0.0, flags=0)
                if bot_port == 2:
                    p2 = bot
                else:
                    p1 = bot
                pending_state = Frame(frame, 0, p1, p2)
                if ack is not None:
                    send(pack_ack(seq, *ack))
                frame += 1
            if now >= next_hb:
                next_hb = now + 0.5
                send(pack_heartbeat(seq, int((now - start) * 1000), version, True, 2 * frame, frame))
                send(pack_control_status(seq, status()))
            # Short naps so incoming inputs get picked up promptly, like the
            # plugin's 1 ms poll.
            time.sleep(0.001 if match else 0.01)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    c = side.c
    log(f"[fake] done: sent {sent}, dropped {dropped}; inputs rx {c.inputs_rx}, applied {c.applied}, "
        f"late {c.late}, repeats {c.repeats}, neutral {c.neutral}, rejected {c.rejected}")
    return side


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4243)
    ap.add_argument("--drop", type=float, default=0.0, help="fraction of outgoing packets to throw away")
    ap.add_argument("--version", default="13.0.4")
    ap.add_argument("--no-match", action="store_true", help="heartbeats only")
    ap.add_argument("--seconds", type=float, default=None)
    ap.add_argument("--bot-port", type=int, default=2, choices=(1, 2))
    ap.add_argument("--latency-ms", type=float, default=0.0, help="hold incoming inputs this long before using them")
    ap.add_argument("--input-drop", type=float, default=0.0, help="fraction of incoming inputs to throw away")
    args = ap.parse_args()
    run(args.host, args.port, args.drop, args.version, not args.no_match, args.seconds,
        args.bot_port, args.latency_ms, args.input_drop)


if __name__ == "__main__":
    main()
