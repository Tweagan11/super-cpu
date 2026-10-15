"""Wire format between the Switch plugin and the PC bridge.

Must match plugin/src/state.rs and docs/protocol.md. Little endian, no padding
beyond what repr(C) produces on the Switch (aarch64).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

# fighter_kind, pos_x, pos_y, vel_x, vel_y, damage, shield_hp, stocks,
# status_kind, (4 bytes padding), motion_kind, anim_frame, facing, jumps_left, flags
FIGHTER_FMT = "<i6fii4xQfiiI"
FRAME_HEADER_FMT = "<Ii"
FIGHTER_SIZE = struct.calcsize(FIGHTER_FMT)
FRAME_SIZE = struct.calcsize(FRAME_HEADER_FMT) + 2 * FIGHTER_SIZE

# stick_x, stick_y, cstick_x, cstick_y (int8, -128..127), buttons (uint32 bitmask), frame (uint32)
INPUT_FMT = "<4bII"
INPUT_SIZE = struct.calcsize(INPUT_FMT)


@dataclass(frozen=True)
class Fighter:
    fighter_kind: int
    pos_x: float
    pos_y: float
    vel_x: float
    vel_y: float
    damage: float
    shield_hp: float
    stocks: int
    status_kind: int
    motion_kind: int
    anim_frame: float
    facing: int
    jumps_left: int
    flags: int

    @property
    def airborne(self) -> bool:
        return bool(self.flags & 1)

    @property
    def in_hitstun(self) -> bool:
        return bool(self.flags & 2)


@dataclass(frozen=True)
class Frame:
    frame: int
    stage_id: int
    bot: Fighter
    opponent: Fighter


@dataclass(frozen=True)
class Inputs:
    stick_x: int = 0
    stick_y: int = 0
    cstick_x: int = 0
    cstick_y: int = 0
    buttons: int = 0


def decode_frame(buf: bytes) -> Frame:
    frame, stage_id = struct.unpack_from(FRAME_HEADER_FMT, buf, 0)
    off = struct.calcsize(FRAME_HEADER_FMT)
    bot = Fighter(*struct.unpack_from(FIGHTER_FMT, buf, off))
    opp = Fighter(*struct.unpack_from(FIGHTER_FMT, buf, off + FIGHTER_SIZE))
    return Frame(frame, stage_id, bot, opp)


def encode_inputs(inp: Inputs, frame: int) -> bytes:
    return struct.pack(INPUT_FMT, inp.stick_x, inp.stick_y, inp.cstick_x, inp.cstick_y, inp.buttons, frame)
