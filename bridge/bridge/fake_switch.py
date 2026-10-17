"""Pretend to be the Switch plugin so the PC side can be tested without hardware.

    python -m bridge.fake_switch                 # heartbeats + a fake 60 fps match
    python -m bridge.fake_switch --no-match      # heartbeats only
    python -m bridge.fake_switch --drop 0.02     # throw away 2% of packets it sends
    python -m bridge.fake_switch --latency-ms 20 # inputs take 20 ms to "arrive"
    python -m bridge.fake_switch --matches 3     # 3 short matches with stock losses, a winner, and gaps
                                                 # (takes inputs and acks too, so bridge.run --eval works)

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

Like plugin v3, every state packet carries both ports' captured inputs
(SSB-13): the bot's port gets what was actually applied (marked as driven by
the PC) while a session is on, and the other port gets plausible "human"
inputs from `human_inputs()`. With no session the bot's port gets the same
kind of synthesized controller input, not driven.

Like plugin v4, it also sends each port's ControlModule inputs. They equal the
captured ones, except for `--cpu-ports 1,2` (comma list of ports): those ports
are "CPUs", which the real final input mapping never sees, so they get no
captured inputs and only the ControlModule block, like a CPU vs CPU match.

SSB-25: it also sends scene packets (kind 7) and takes menu presses (kind 8).
Scenes go CSS -> match -> results -> CSS. With `menus=True` (`--menus`) the
loop waits for the bridge like the real game would: a match ends on the
results scene until an A press arrives, then the CSS waits for + before the
next match (`bridge.run --auto-rematch`). `menu_ignore=N` makes the results
screen swallow the first N presses, to exercise the retry/backoff. Without
`menus` the scenes still change on the timer and presses are only recorded.

`--version 13.0.3` pretends the Switch runs the wrong game build: like the
real plugin, it then sends heartbeats only.
"""
from __future__ import annotations

import argparse
import heapq
import math
import random
import socket
import time
from dataclasses import dataclass, replace

from . import buttons as B
from . import menu_buttons as MB
from .protocol import Fighter, Frame, Inputs
from .scenes import FAKE_CSS, FAKE_MATCH, FAKE_RESULTS
from .stages import STAGE_ID_BATTLEFIELD, STAGE_ID_UNKNOWN
from .statuses import STATUS_NAMES
from .wire import (
    FLAG_HOOK_INSTALLED,
    FLAG_RECEIVING,
    FLAG_SESSION_ACTIVE,
    KIND_INPUT,
    KIND_MENU,
    SCENE_FLAG_MENU_HOOK,
    SCENE_FLAG_SCENE_HOOK,
    BadPacket,
    ControlStatus,
    Header,
    pack_ack,
    pack_control_status,
    pack_heartbeat,
    pack_log,
    pack_scene,
    pack_state,
    unpack_header,
    unpack_input,
    unpack_menu,
)

ROBOT = 0x2D
FRAME_TIME = 1 / 60
EXPECTED_GAME_VERSION = "13.0.4"  # plugin/src/version.rs

# Same numbers as plugin/src/input.rs
REPEAT_FRAMES = 6
SESSION_TIMEOUT = 2.0
RING = 32
RESYNC_AFTER = 30

WALK_SPEED = 1.2  # units per frame at full tilt


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


def human_inputs(i: int, side: int) -> Inputs:
    """Plausible controller input for frame i: the stick sweeps back and
    forth, a jab every 40 frames (held 3), a jump every 90, shield now and
    then, and a c-stick flick every 120. Deterministic, so tests can check it."""
    phase = i + (0 if side < 0 else 17)
    buttons = 0
    if phase % 40 < 3:
        buttons |= B.ATTACK
    if phase % 90 < 2:
        buttons |= B.JUMP
    if phase % 150 >= 140:
        buttons |= B.GUARD | B.GUARD_HOLD
    cx = 127 if phase % 120 < 4 else 0
    return Inputs(
        stick_x=round(100 * math.sin(phase / 25)), stick_y=-60 if phase % 200 < 10 else 0,
        cstick_x=cx, cstick_y=0, buttons=buttons | (B.CSTICK_ON if cx else 0),
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
        self.scenes: list[str] = []  # scenes visited, in order (SSB-25)
        self.menu_presses: list[tuple[str, int]] = []  # (scene, button mask) per press received
        self.menu_applied = 0
        self.menu_last_seq = 0xFFFFFFFF

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
        bot_port: int = 2, latency_ms: float = 0.0, input_drop: float = 0.0, quiet: bool = False,
        matches: int | None = None, match_seconds: float = 6.0, gap: float = 2.5,
        winners: tuple[int, ...] = (2, 2, 1), cpu_ports: tuple[int, ...] = (), expert: bool = False,
        menus: bool = False, menu_ignore: int = 0) -> FakeInputSide:
    """`matches=N` plays N scripted matches (stock losses, a winner, gaps for
    the results screen) back to back, then stops; otherwise one endless
    drifting match. Inputs and acks work the same either way."""
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
    if match and version != EXPECTED_GAME_VERSION:
        # same rule as plugin/src/version.rs: wrong build, no state
        log(f"[fake] version {version} != {EXPECTED_GAME_VERSION}, heartbeats only")
        match = False
    log(f"[fake] sending to {host}:{port}, {'with' if match else 'without'} a match, dropping {drop:.0%}, "
        f"bot on port {bot_port}, input latency {latency_ms:.0f} ms"
        + (f", {matches} matches of {match_seconds}s with {gap}s gaps" if matches else ""))
    script: list[Frame] | None = None  # current scripted match, None = endless drift
    match_no = 0
    gap_until: float | None = None
    playing = True  # a match is running (False on the results screen / CSS)
    scene_name = FAKE_MATCH if match else FAKE_CSS
    scene_changes = 1
    scene_sent = 0
    next_scene = start
    side.scenes.append(scene_name)
    awaiting: str | None = None  # menus mode: "results" or "start"
    ignored = 0
    end_at: float | None = None

    def set_scene(name: str) -> None:
        nonlocal scene_name, scene_changes
        scene_name = name
        scene_changes += 1
        side.scenes.append(name)
        log(f"[fake] scene {name}")

    def on_menu(mask: int, press_seq: int, now: float) -> None:
        nonlocal awaiting, ignored, match_no, script, frame, playing, next_tick, end_at
        side.menu_presses.append((scene_name, mask))
        side.menu_applied += 1
        side.menu_last_seq = press_seq
        if not menus:
            return
        if awaiting == "results" and mask & MB.A:
            if ignored < menu_ignore:
                ignored += 1
                return
            match_no += 1
            set_scene(FAKE_CSS)
            if match_no >= (matches or 0):
                awaiting = None
                end_at = now + 1.5
            else:
                awaiting = "start"
        elif awaiting == "start" and mask & MB.PLUS:
            script = next_script()
            frame = 0
            playing = True
            next_tick = now
            awaiting = None
            set_scene(FAKE_MATCH)

    def next_script() -> list[Frame] | None:
        winner = winners[match_no % len(winners)]
        log(f"[fake] match {match_no + 1}/{matches}: P{winner} wins")
        return scripted_match(int(match_seconds * 60), winner, winner_losses=match_no % 3,
                              stage_id=STAGE_ID_BATTLEFIELD)

    if matches:
        script = next_script()

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
                kind = unpack_header(buf).kind
                if kind == KIND_MENU:
                    m = unpack_menu(buf)
                    on_menu(m.mask, m.press_seq, now)
                    continue
                if kind != KIND_INPUT:
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
            Header(3, 5, 0), bot_port, bot_port - 1, flags, other_player_idx=2 - bot_port,
            fim_calls=2 * frame, idx_seen=0b11, inputs_rx=c.inputs_rx, applied=c.applied, late=c.late,
            rejected=c.rejected, repeats=c.repeats, neutral=c.neutral,
        )

    next_tick = start
    try:
        while seconds is None or time.monotonic() - start < seconds:
            now = time.monotonic()
            pump(now)
            if end_at is not None and now >= end_at:
                break
            if gap_until is not None and scene_name == FAKE_RESULTS and now >= gap_until - 0.8:
                set_scene(FAKE_CSS)
            if gap_until is not None and now >= gap_until:
                gap_until = None
                match_no += 1
                if match_no >= (matches or 0):
                    break
                script = next_script()
                frame = 0
                next_tick = now
                playing = True
                set_scene(FAKE_MATCH)
            if match and playing and now >= next_tick:
                next_tick += FRAME_TIME
                if next_tick < now - 0.1:
                    next_tick = now  # fell way behind (debugger, sleep); don't burst
                t = now - start
                # 1. the game maps every controller for this frame
                inputs, ack = side.step(frame, now)
                # 2. the fighters update; the first one flushes last frame's state
                if pending_state is not None:
                    send(pack_state(seq, pending_state))
                    pending_state = None
                if script is not None:
                    p1, p2 = script[frame].bot, script[frame].opponent
                else:
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
                # 3. what each port's fighter read this frame (SSB-13 capture)
                human = [human_inputs(frame, -1), human_inputs(frame, 1)]
                driven = [False, False]
                if inputs is not None:
                    human[bot_port - 1] = inputs
                    driven[bot_port - 1] = True
                # v4: every fighter's ControlModule read; a CPU port has no capture
                cm = (human[0], human[1])
                if expert and driven[bot_port - 1]:
                    # drive_cpu DAgger: the bot's CM block is the CPU AI's
                    # decision, which differs from what was played (shifted
                    # phase of the same plausible controller, so it is
                    # non-trivial and disagrees most of the time)
                    exp = human_inputs(frame + 31, bot_port * 2 - 3)
                    cm = tuple(exp if i == bot_port - 1 else human[i] for i in range(2))
                fim = tuple(None if (i + 1) in cpu_ports and not driven[i] else human[i] for i in range(2))
                pending_state = Frame(frame, STAGE_ID_BATTLEFIELD, p1, p2,
                                      inputs=fim, driven=(driven[0], driven[1]), cm_inputs=cm,
                                      cm_expert=expert and driven[bot_port - 1])
                if ack is not None:
                    send(pack_ack(seq, *ack))
                frame += 1
                if script is not None and frame >= len(script):
                    # results screen: last state goes out, then heartbeats only
                    send(pack_state(seq, pending_state))
                    pending_state = None
                    playing = False
                    set_scene(FAKE_RESULTS)
                    if menus:
                        awaiting, ignored = "results", 0
                    else:
                        gap_until = now + gap
            if scene_changes != scene_sent or now >= next_scene:
                scene_sent = scene_changes
                next_scene = now + 1.0
                send(pack_scene(seq, scene_name, scene_changes,
                                SCENE_FLAG_SCENE_HOOK | (SCENE_FLAG_MENU_HOOK if menus else 0),
                                side.menu_applied, side.menu_last_seq))
            if now >= next_hb:
                next_hb = now + 0.5
                send(pack_heartbeat(seq, int((now - start) * 1000), version, True, 2 * frame, frame))
                send(pack_control_status(seq, status()))
                # like the plugin: recent log lines resent every few seconds
                if int((now - start) * 2) % 10 == 0:
                    send(pack_log(seq, 0, "fake switch: inputs on"))
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
    ap.add_argument("--matches", type=int, default=None, help="play N short scripted matches, then stop")
    ap.add_argument("--match-seconds", type=float, default=6.0)
    ap.add_argument("--gap", type=float, default=2.5, help="seconds between matches (must beat the 1.5 s end timeout)")
    ap.add_argument("--menus", action="store_true",
                    help="wait for menu presses between matches (A on results, + on the CSS), for bridge.run --auto-rematch")
    ap.add_argument("--menu-ignore", type=int, default=0, help="with --menus: the results screen ignores this many A presses")
    ap.add_argument("--winners", default="2,2,1", help="winning port per match, cycled, e.g. 2,2,1")
    ap.add_argument("--expert", action="store_true",
                    help="mimic drive_cpu DAgger: the driven port's CM block is a CPU AI decision that differs from the played input (CAP_BOT_EXPERT)")
    ap.add_argument("--cpu-ports", default="", help="ports played by CPUs, e.g. 1,2: control module inputs only")
    args = ap.parse_args()
    winners = tuple(int(w) for w in args.winners.split(","))
    cpu_ports = tuple(int(w) for w in args.cpu_ports.split(",") if w)
    run(args.host, args.port, args.drop, args.version, not args.no_match, args.seconds,
        args.bot_port, args.latency_ms, args.input_drop, matches=args.matches,
        match_seconds=args.match_seconds, gap=args.gap, winners=winners, cpu_ports=cpu_ports, expert=args.expert,
        menus=args.menus, menu_ignore=args.menu_ignore)


if __name__ == "__main__":
    main()
