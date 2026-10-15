"""Synthetic logged matches with known labels, for testing the Phase 2 pipeline
before real human inputs exist.

Frames come from `bridge.fake_switch.fake_fighter` (two R.O.B.s hopping and
drifting), with a random time offset and phase per match and port so held-out
matches aren't copies of training ones. They go through the real
`MatchWatcher` + `MatchLogger`, so the files are byte for byte the format the
bridge writes. Some packets are dropped (gaps in `frame`) and some frames get
no inputs recorded (`input_valid = 0`).

The bot's "human" inputs come from a rule run on the received history, the
same way a live policy would see it, and get recorded with
`MatchLogger.record_inputs()`:

- `rule` (default): `RulePolicy` below. Stateless, so a model that sees the
  same frames can in principle get 100% at delay 0. Uses all three heads.
- `scripted`: `bridge.scripted.ScriptedRobPolicy`. Has hidden state (attack
  cooldown, double jump used) so it can't be cloned perfectly from features.
- `walk`: `bridge.policy.WalkTowardPolicy`. Main stick only.
"""
from __future__ import annotations

import math
import random
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from bridge import buttons as B
from bridge.fake_switch import ATTACK_S3, fake_fighter
from bridge.logger import MatchLogger
from bridge.matches import MatchWatcher
from bridge.policy import History, WalkTowardPolicy
from bridge.protocol import Frame, Inputs
from bridge.scripted import ATTACK_STATUSES, ScriptedRobPolicy
from bridge.stages import STAGE_ID_BATTLEFIELD, STAGE_ID_FINAL_DESTINATION
from bridge.wire import pack_heartbeat, pack_state




class RulePolicy:
    """Known, stateless rule for the smoke test. Every output is an exact
    member of the action space, so encode -> decode is lossless.

    - main stick: toward the opponent, full tilt if far, half tilt if mid, center if close.
      Up-forward diagonal if they're well above.
    - c-stick: up when the opponent is close and above, down when close and below.
    - buttons: guard if they're attacking and close, attack if close,
      jump if they're above and we're grounded, else none.
    """

    def __init__(self, bot_port: int = 1) -> None:
        self.bot_port = bot_port

    def act(self, history: Sequence[Frame]) -> Inputs:
        f = history[-1]
        me, opp = (f.bot, f.opponent) if self.bot_port == 1 else (f.opponent, f.bot)
        dx, dy = opp.pos_x - me.pos_x, opp.pos_y - me.pos_y
        adx = abs(dx)
        s = 1 if dx > 0 else -1
        if adx > 40:
            stick = (127 * s, 0) if dy < 15 else (90 * s, 90)
        elif adx > 15:
            stick = (64 * s, 0)
        else:
            stick = (0, 0)
        cstick = (0, 0)
        if adx < 25 and dy > 10:
            cstick = (0, 127)
        elif adx < 25 and dy < -10:
            cstick = (0, -127)
        close = math.hypot(dx, dy) < 25
        if close and opp.status_kind in ATTACK_STATUSES:
            btn = B.GUARD
        elif close:
            btn = B.ATTACK
        elif dy > 15 and not me.airborne:
            btn = B.JUMP
        else:
            btn = B.NONE
        return Inputs(stick[0], stick[1], cstick[0], cstick[1], btn)


def make_rule(name: str, bot_port: int):
    if name == "rule":
        return RulePolicy(bot_port)
    if name == "scripted":
        return ScriptedRobPolicy(stage="fd", bot_port=bot_port)
    if name == "walk":
        if bot_port != 1:
            raise ValueError("walk only knows frame.bot, use bot_port 1")
        return WalkTowardPolicy()
    raise ValueError(f"unknown rule {name!r}")


def synthetic_frames(n_frames: int, rng: random.Random, stage_id: int) -> list[Frame]:
    """One fake match: fake_fighter motion with per-port random time offsets
    and scale, and the opponent swinging (ATTACK_S3) in random bursts."""
    offs = [rng.uniform(0, 60), rng.uniform(0, 60)]
    scale = [rng.uniform(0.6, 1.4), rng.uniform(0.6, 1.4)]
    shift = [rng.uniform(-20, 20), rng.uniform(-20, 20)]
    attack_period = rng.randint(30, 90)
    frames = []
    for i in range(n_frames):
        t = i / 60
        fs = []
        for port, side in ((0, -1), (1, 1)):
            f = fake_fighter(t * scale[port] + offs[port], side)
            f = replace(f, pos_x=f.pos_x * scale[port] + shift[port], stocks=3)
            if port == 1 and (i // attack_period) % 3 == 1:
                f = replace(f, status_kind=ATTACK_S3)
            fs.append(f)
        frames.append(Frame(i, stage_id, fs[0], fs[1]))
    return frames


def write_synthetic_matches(
    out_dir: Path | str,
    n_matches: int = 8,
    n_frames: int = 1200,
    rule: str = "rule",
    bot_port: int = 1,
    drop: float = 0.02,
    no_input: float = 0.03,
    seed: int = 0,
    version: str = "13.0.4",
) -> list[Path]:
    """Write `n_matches` synthetic logged matches into out_dir. Returns the .npz paths."""
    rng = random.Random(seed)
    out: list[Path] = []
    logger = MatchLogger(out_dir=out_dir, expect_version="13.0.4", echo=lambda s: None,
                         controllers=("bot:" + rule, "cpu:fake") if bot_port == 1 else ("cpu:fake", "bot:" + rule),
                         on_logged=lambda r: out.append(r.path) if r.path else None)
    watcher = MatchWatcher([logger])
    now = 0.0
    seq = 0
    for m in range(n_matches):
        stage_id = STAGE_ID_BATTLEFIELD if m % 2 == 0 else STAGE_ID_FINAL_DESTINATION
        frames = synthetic_frames(n_frames, rng, stage_id)
        policy = make_rule(rule, bot_port)
        hist = History()
        watcher.packet(pack_heartbeat(seq, int(now * 1000), version), now)
        seq += 1
        for f in frames:
            now += 1 / 60
            seq += 1
            if f.frame > 0 and rng.random() < drop:
                continue  # dropped packet: the logger never sees this frame
            watcher.packet(pack_state(seq, f), now)
            # The rule sees what arrived (decoded, so f32 like the log), same as live.
            inp = policy.act(hist.push(_wire_round_trip(f)))
            if rng.random() >= no_input:
                logger.record_inputs(bot_port, f.frame, inp)
        now += 2.0
        watcher.poll(now)
    return out


def _wire_round_trip(f: Frame) -> Frame:
    from bridge.wire import unpack_state
    return unpack_state(pack_state(0, f))[1]
