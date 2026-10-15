"""Synthetic frames for tests."""
from __future__ import annotations

from dataclasses import replace

from bridge.protocol import Fighter, Frame
from bridge.statuses import STATUS_NAMES

S = {v: k for k, v in STATUS_NAMES.items()}
ROBOT = 0x2D


def fighter(**kw) -> Fighter:
    base = dict(
        fighter_kind=ROBOT, pos_x=0.0, pos_y=0.0, vel_x=0.0, vel_y=0.0, damage=0.0,
        shield_hp=0.0, stocks=3, status_kind=S["WAIT"], motion_kind=0, anim_frame=0.0,
        facing=1, jumps_left=0, flags=0,
    )
    base.update(kw)
    return Fighter(**base)


def frame(n: int = 0, bot: Fighter | None = None, opp: Fighter | None = None, stage_id: int = 0) -> Frame:
    return Frame(n, stage_id, bot or fighter(), opp or fighter(pos_x=50.0, facing=-1))


def airborne(f: Fighter) -> Fighter:
    return replace(f, flags=f.flags | 1)
