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

# The input packet layout lives in wire.py (pack_input), next to the header.


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


def decode_frame(buf: bytes, offset: int = 0, bot_index: int = 0) -> Frame:
    """Decode a frame body. The packet always has port 1 then port 2;
    `bot_index` (bot port - 1, so 0 or 1) says which one is `bot`."""
    frame, stage_id = struct.unpack_from(FRAME_HEADER_FMT, buf, offset)
    off = offset + struct.calcsize(FRAME_HEADER_FMT)
    p1 = Fighter(*struct.unpack_from(FIGHTER_FMT, buf, off))
    p2 = Fighter(*struct.unpack_from(FIGHTER_FMT, buf, off + FIGHTER_SIZE))
    return Frame(frame, stage_id, p2, p1) if bot_index == 1 else Frame(frame, stage_id, p1, p2)


def encode_frame(f: Frame, bot_index: int = 0) -> bytes:
    def fighter(x: Fighter) -> bytes:
        return struct.pack(
            FIGHTER_FMT, x.fighter_kind, x.pos_x, x.pos_y, x.vel_x, x.vel_y, x.damage,
            x.shield_hp, x.stocks, x.status_kind, x.motion_kind, x.anim_frame, x.facing,
            x.jumps_left, x.flags,
        )
    p1, p2 = (f.opponent, f.bot) if bot_index == 1 else (f.bot, f.opponent)
    return struct.pack(FRAME_HEADER_FMT, f.frame, f.stage_id) + fighter(p1) + fighter(p2)
