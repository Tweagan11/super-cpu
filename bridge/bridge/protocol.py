"""Wire format between the Switch plugin and the PC bridge.

Must match plugin/src/state.rs and docs/protocol.md. Little endian, no padding
beyond what repr(C) produces on the Switch (aarch64).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

# fighter_kind, pos_x, pos_y, vel_x, vel_y, damage, shield_hp, stocks,
# status_kind, (4 bytes padding), motion_kind, anim_frame, facing, jumps_left, flags
FIGHTER_FMT = "<i6fii4xQfiiI"
FRAME_HEADER_FMT = "<Ii"
FIGHTER_SIZE = struct.calcsize(FIGHTER_FMT)
# v3 (SSB-13): after the fighters, input_flags then each port's captured
# final mapped inputs (4 x i8 sticks, u32 buttons), port 1 first.
CAPTURE_FMT = "<I4bI4bI"
CAPTURE_SIZE = struct.calcsize(CAPTURE_FMT)  # 20
FRAME_SIZE = struct.calcsize(FRAME_HEADER_FMT) + 2 * FIGHTER_SIZE + CAPTURE_SIZE

# input_flags bits (plugin/src/input.rs CAP_*)
CAP_P1_VALID = 1
CAP_P2_VALID = 2
CAP_P1_DRIVEN = 4
CAP_P2_DRIVEN = 8

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

    @property
    def invincible(self) -> bool:
        """Always False for now: the plugin doesn't set bit 2 yet."""
        return bool(self.flags & 4)


@dataclass(frozen=True)
class Inputs:
    stick_x: int = 0
    stick_y: int = 0
    cstick_x: int = 0
    cstick_y: int = 0
    buttons: int = 0


@dataclass(frozen=True)
class Frame:
    frame: int
    stage_id: int
    bot: Fighter
    opponent: Fighter
    # Which port `bot` is, as an index (bot port - 1). Set by decode_frame.
    bot_index: int = 0
    # SSB-13: the final mapped inputs each port's fighter read this frame,
    # in PORT order (index 0 = port 1, not bot/opponent). None = not captured.
    inputs: tuple[Inputs | None, Inputs | None] = (None, None)
    # True where the PC (the bot) wrote that port's inputs, port order.
    driven: tuple[bool, bool] = (False, False)

    @property
    def by_port(self) -> tuple[Fighter, Fighter]:
        """(port 1, port 2), whichever one is the bot."""
        return (self.opponent, self.bot) if self.bot_index == 1 else (self.bot, self.opponent)

    @property
    def bot_inputs(self) -> Inputs | None:
        return self.inputs[self.bot_index] if self.bot_index in (0, 1) else None

    @property
    def opponent_inputs(self) -> Inputs | None:
        return self.inputs[1 - self.bot_index] if self.bot_index in (0, 1) else None


def decode_frame(buf: bytes, offset: int = 0, bot_index: int = 0) -> Frame:
    """Decode a frame body. The packet always has port 1 then port 2;
    `bot_index` (bot port - 1, so 0 or 1) says which one is `bot`."""
    frame, stage_id = struct.unpack_from(FRAME_HEADER_FMT, buf, offset)
    off = offset + struct.calcsize(FRAME_HEADER_FMT)
    p1 = Fighter(*struct.unpack_from(FIGHTER_FMT, buf, off))
    p2 = Fighter(*struct.unpack_from(FIGHTER_FMT, buf, off + FIGHTER_SIZE))
    v = struct.unpack_from(CAPTURE_FMT, buf, off + 2 * FIGHTER_SIZE)
    flags = v[0]
    inputs = (
        Inputs(*v[1:6]) if flags & CAP_P1_VALID else None,
        Inputs(*v[6:11]) if flags & CAP_P2_VALID else None,
    )
    driven = (bool(flags & CAP_P1_DRIVEN), bool(flags & CAP_P2_DRIVEN))
    bot, opp = (p2, p1) if bot_index == 1 else (p1, p2)
    return Frame(frame, stage_id, bot, opp, 1 if bot_index == 1 else 0, inputs, driven)


def capture_flags(f: Frame) -> int:
    flags = 0
    if f.inputs[0] is not None:
        flags |= CAP_P1_VALID
    if f.inputs[1] is not None:
        flags |= CAP_P2_VALID
    if f.driven[0]:
        flags |= CAP_P1_DRIVEN
    if f.driven[1]:
        flags |= CAP_P2_DRIVEN
    return flags


def encode_frame(f: Frame, bot_index: int | None = None) -> bytes:
    """`bot_index` defaults to the frame's own. Passing it says which wire
    slot `f.bot` goes in (the inputs stay in port order either way)."""
    if bot_index is None:
        bot_index = f.bot_index
    def fighter(x: Fighter) -> bytes:
        return struct.pack(
            FIGHTER_FMT, x.fighter_kind, x.pos_x, x.pos_y, x.vel_x, x.vel_y, x.damage,
            x.shield_hp, x.stocks, x.status_kind, x.motion_kind, x.anim_frame, x.facing,
            x.jumps_left, x.flags,
        )
    p1, p2 = (f.opponent, f.bot) if bot_index == 1 else (f.bot, f.opponent)
    a = f.inputs[0] or Inputs()
    b = f.inputs[1] or Inputs()
    tail = struct.pack(
        CAPTURE_FMT, capture_flags(f),
        a.stick_x, a.stick_y, a.cstick_x, a.cstick_y, a.buttons & 0xFFFFFFFF,
        b.stick_x, b.stick_y, b.cstick_x, b.cstick_y, b.buttons & 0xFFFFFFFF,
    )
    return struct.pack(FRAME_HEADER_FMT, f.frame, f.stage_id) + fighter(p1) + fighter(p2) + tail
