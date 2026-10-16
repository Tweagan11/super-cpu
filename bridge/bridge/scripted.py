"""Scripted R.O.B. baseline (SSB-11). Rules, no learning.

Priority each frame, first match wins:

1. Recover: offstage and below stage level -> drift to center, double jump
   once, then up-special (Robo Burner, held) toward center.
2. Shield: grounded, opponent close and in an attack status -> hold shield.
3. Attack: opponent inside the spacing range -> tilt toward them (up tilt if
   they're above), aerial if we're airborne.
4. Approach: opponent onstage but out of range -> walk/dash toward them,
   short hop if they're up on a Battlefield platform.
5. Neutral: nothing, or drift back toward center near a ledge.

What it can see (docs/protocol.md "Live in the first cut"): pos, damage,
stocks, status, facing, airborne. vel, shield_hp and jumps_left come through
as 0 for now. The bot only uses those once it has seen a nonzero value in its
history, so it works the same with or without them.

Stage geometry is the weak spot. The layout (Battlefield or FD/Omega) comes
from the streamed stage_id via `resolve_layout`, falling back to config for
other stages. The ledge/platform numbers in stages.py are estimates, so the
recovery and ledge rules need measured values before they're any good.

Config, because `bridge.run --policy scripted` builds policies with no args:
keyword args win, then env vars SCPU_STAGE (battlefield, fd, omega) and
SCPU_BOT_PORT (1 or 2), then the defaults (fd, frame.bot is the bot).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Sequence

from . import buttons as B
from .kinds import FIGHTER_KINDS
from .protocol import Fighter, Frame, Inputs
from .stages import StageLayout, layout_by_name, resolve_layout
from .statuses import STATUS_NAMES

ROBOT = next(k for k, v in FIGHTER_KINDS.items() if v == "robot")
_STATUS = {v: k for k, v in STATUS_NAMES.items()}

# Statuses that mean "the opponent is swinging at us". Specials count since
# most of them are attacks; grabs don't since shielding a grab is a mistake.
ATTACK_STATUSES = frozenset(
    k for k, v in STATUS_NAMES.items()
    if v.startswith("ATTACK") or v.startswith("SPECIAL_")
) | {_STATUS["LANDING_ATTACK_AIR"]}

DEAD_STATUSES = frozenset({_STATUS["DEAD"], _STATUS["REBIRTH"], _STATUS["STANDBY"], _STATUS["ENTRY"]})
SPECIAL_HI = _STATUS["SPECIAL_HI"]
JUMP_AERIAL = _STATUS["JUMP_AERIAL"]
HALF = 64


@dataclass
class ScriptedConfig:
    stage: str = "fd"
    bot_port: int | None = None    # None: trust frame.bot. 2: swap bot/opponent
    attack_range: float = 18.0     # |dx| to start swinging
    attack_height: float = 20.0    # |dy| still counted as "in range"
    shield_range: float = 22.0     # distance where an opponent attack is a threat
    ledge_margin: float = 8.0      # don't walk closer than this to a ledge
    min_shield_hp: float = 15.0    # skip shielding below this (only if shield_hp is live)
    attack_cooldown: int = 12      # frames between attack presses


class ScriptedRobPolicy:
    """Satisfies bridge.policy.Policy."""

    def __init__(self, stage: str | None = None, bot_port: int | str | None = None, **overrides) -> None:
        stage = stage or os.environ.get("SCPU_STAGE") or "fd"
        port = bot_port if bot_port is not None else os.environ.get("SCPU_BOT_PORT")
        self.cfg = ScriptedConfig(stage=stage, bot_port=int(port) if port else None, **overrides)
        self.fallback_layout = layout_by_name(self.cfg.stage)
        self.reset()

    def reset(self) -> None:
        self.last_buttons = 0
        self.used_double_jump = False
        self.attack_wait = 0
        self.live_vel = self.live_shield = self.live_jumps = False
        self.last_reason = ""

    # helpers

    def _orient(self, f: Frame) -> tuple[Fighter, Fighter]:
        # Frames decoded with a bot port (bridge.run) already have frame.bot
        # as the bot; only swap when the frame says the bot is the other port.
        if self.cfg.bot_port in (1, 2) and f.bot_index != self.cfg.bot_port - 1:
            return f.opponent, f.bot
        return f.bot, f.opponent

    def _press(self, button: int) -> int:
        """Buttons that need a fresh press: release for a frame if held last frame."""
        return 0 if self.last_buttons & button else button

    def _toward(self, from_x: float, to_x: float, amount: int = B.STICK_MAX) -> int:
        return amount if to_x > from_x else -amount

    def _out(self, reason: str, stick_x: int = 0, stick_y: int = 0, buttons: int = 0) -> Inputs:
        self.last_reason = reason
        self.last_buttons = buttons
        return Inputs(stick_x=stick_x, stick_y=stick_y, buttons=buttons)

    # policy

    def act(self, history: Sequence[Frame]) -> Inputs:
        if not history:
            return self._out("no frames")
        f = history[-1]
        me, opp = self._orient(f)
        stage = resolve_layout(f.stage_id, self.fallback_layout)
        self.live_vel |= me.vel_x != 0 or me.vel_y != 0
        self.live_shield |= me.shield_hp != 0
        self.live_jumps |= me.jumps_left != 0
        if self.attack_wait:
            self.attack_wait -= 1

        if me.status_kind in DEAD_STATUSES:
            self.used_double_jump = False
            return self._out("dead")
        if not me.airborne:
            self.used_double_jump = False

        if stage.offstage(me.pos_x) and stage.below_stage(me.pos_y):
            return self._recover(me, stage)

        dx = opp.pos_x - me.pos_x
        dy = opp.pos_y - me.pos_y
        dist = (dx * dx + dy * dy) ** 0.5
        opp_dead = opp.status_kind in DEAD_STATUSES

        if (not me.airborne and not opp_dead and dist < self.cfg.shield_range
                and opp.status_kind in ATTACK_STATUSES and self._shield_ok(me)):
            return self._out("shield", buttons=B.GUARD)

        if not opp_dead and abs(dx) < self.cfg.attack_range and abs(dy) < self.cfg.attack_height:
            return self._attack(me, dx, dy)

        if not opp_dead and not stage.offstage(opp.pos_x):
            return self._approach(me, opp, dx, dy, stage)

        return self._neutral(me, stage)

    def _shield_ok(self, me: Fighter) -> bool:
        return not self.live_shield or me.shield_hp >= self.cfg.min_shield_hp

    def _recover(self, me: Fighter, stage: StageLayout) -> Inputs:
        stick_x = self._toward(me.pos_x, stage.center_x)
        have_jump = me.jumps_left > 0 if self.live_jumps else not self.used_double_jump
        if me.status_kind == SPECIAL_HI:
            # Robo Burner: keep holding special and steer.
            return self._out("recover: up-b", stick_x, B.STICK_MAX, B.SPECIAL)
        if have_jump and me.status_kind != JUMP_AERIAL:
            press = self._press(B.JUMP)
            if press:
                self.used_double_jump = True
            return self._out("recover: jump", stick_x, 0, press)
        if self.live_vel and me.vel_y > 0:
            return self._out("recover: rising", stick_x)
        return self._out("recover: up-b", stick_x, B.STICK_MAX, self._press(B.SPECIAL))

    def _attack(self, me: Fighter, dx: float, dy: float) -> Inputs:
        if self.attack_wait:
            return self._out("attack: wait")
        press = self._press(B.ATTACK)
        if not press:
            return self._out("attack: release")
        self.attack_wait = self.cfg.attack_cooldown
        if dy > 10 and abs(dx) < 10:
            return self._out("attack: up", 0, HALF, press)
        if me.airborne:
            return self._out("attack: aerial", self._toward(0, dx, HALF), 0, press)
        return self._out("attack: tilt", self._toward(0, dx, HALF), 0, press)

    def _approach(self, me: Fighter, opp: Fighter, dx: float, dy: float, stage: StageLayout) -> Inputs:
        stick_x = self._toward(0, dx)
        # Don't walk off. Only blocks moving outward, so we can still walk back in.
        outward = (me.pos_x - stage.center_x) * dx > 0
        if outward and not me.airborne and stage.offstage(me.pos_x, margin=self.cfg.ledge_margin):
            return self._out("approach: at ledge")
        on_platform = any(p.left <= opp.pos_x <= p.right and opp.pos_y >= p.y - 2 for p in stage.platforms)
        if on_platform and dy > 15 and abs(dx) < 30 and not me.airborne:
            return self._out("approach: hop to platform", stick_x, 0, self._press(B.JUMP))
        return self._out("approach", stick_x)

    def _neutral(self, me: Fighter, stage: StageLayout) -> Inputs:
        if stage.offstage(me.pos_x, margin=self.cfg.ledge_margin * 2):
            return self._out("neutral: back to center", self._toward(me.pos_x, stage.center_x, HALF))
        return self._out("neutral")
