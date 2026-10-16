"""UDP packet header and heartbeat, shared by every packet kind.

Must match plugin/src/net.rs and docs/protocol.md.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .protocol import FRAME_SIZE, Frame, Inputs, decode_frame, encode_frame

MAGIC = b"SCPU"
PROTO_VERSION = 3  # v2: heartbeat carries hook health. Kinds 2, 4, 5 added without a bump.
# v3: state carries both ports' captured inputs (SSB-13), control status byte 15
# is the other port's player_idx.

KIND_STATE = 1
KIND_INPUT = 2
KIND_HEARTBEAT = 3
KIND_ACK = 4
KIND_CONTROL_STATUS = 5
KIND_LOG = 6  # plugin log line (plugin/src/log.rs)

HEADER_FMT = "<4sHHI"  # magic, proto_version, kind, seq
HEADER_LEN = struct.calcsize(HEADER_FMT)  # 12

HEARTBEAT_FMT = "<Q8sIII"  # uptime_ms, game version (NUL padded), hook_installed, fighter_calls, frames_queued
HEARTBEAT_LEN = HEADER_LEN + struct.calcsize(HEARTBEAT_FMT)  # 40


class BadPacket(ValueError):
    pass


@dataclass(frozen=True)
class Header:
    proto_version: int
    kind: int
    seq: int


@dataclass(frozen=True)
class Heartbeat:
    header: Header
    uptime_ms: int
    game_version: str
    hook_installed: bool = False
    fighter_calls: int = 0
    frames_queued: int = 0


def pack_header(kind: int, seq: int) -> bytes:
    return struct.pack(HEADER_FMT, MAGIC, PROTO_VERSION, kind, seq & 0xFFFFFFFF)


def unpack_header(buf: bytes) -> Header:
    if len(buf) < HEADER_LEN:
        raise BadPacket(f"too short: {len(buf)} bytes")
    magic, ver, kind, seq = struct.unpack_from(HEADER_FMT, buf, 0)
    if magic != MAGIC:
        raise BadPacket(f"bad magic {magic!r}")
    if ver != PROTO_VERSION:
        raise BadPacket(f"protocol v{ver}, expected v{PROTO_VERSION}")
    return Header(ver, kind, seq)


def pack_heartbeat(
    seq: int, uptime_ms: int, game_version: str,
    hook_installed: bool = False, fighter_calls: int = 0, frames_queued: int = 0,
) -> bytes:
    return pack_header(KIND_HEARTBEAT, seq) + struct.pack(
        HEARTBEAT_FMT, uptime_ms, game_version.encode()[:8],
        int(hook_installed), fighter_calls & 0xFFFFFFFF, frames_queued & 0xFFFFFFFF,
    )


STATE_LEN = HEADER_LEN + FRAME_SIZE  # 168 (v3)


def pack_state(seq: int, frame: Frame, bot_index: int | None = None) -> bytes:
    return pack_header(KIND_STATE, seq) + encode_frame(frame, bot_index)


def unpack_state(buf: bytes, bot_index: int = 0) -> tuple[Header, Frame]:
    """`bot_index` is which of the two fighter blocks is the bot (bot port - 1)."""
    h = unpack_header(buf)
    if h.kind != KIND_STATE or len(buf) < STATE_LEN:
        raise BadPacket("not a state packet")
    return h, decode_frame(buf, HEADER_LEN, bot_index)


# --- Input (kind 2, PC to Switch) -------------------------------------------
# for_frame, apply_at, stick_x, stick_y, cstick_x, cstick_y, buttons
INPUT_FMT = "<II4bI"
INPUT_LEN = HEADER_LEN + struct.calcsize(INPUT_FMT)  # 28


@dataclass(frozen=True)
class InputPacket:
    header: Header
    for_frame: int
    apply_at: int
    inputs: Inputs


def _i8(v: int) -> int:
    return max(-128, min(127, int(v)))


def pack_input(seq: int, for_frame: int, apply_at: int, inp: Inputs) -> bytes:
    return pack_header(KIND_INPUT, seq) + struct.pack(
        INPUT_FMT, for_frame & 0xFFFFFFFF, apply_at & 0xFFFFFFFF,
        _i8(inp.stick_x), _i8(inp.stick_y), _i8(inp.cstick_x), _i8(inp.cstick_y),
        inp.buttons & 0xFFFFFFFF,
    )


def unpack_input(buf: bytes) -> InputPacket:
    h = unpack_header(buf)
    if h.kind != KIND_INPUT or len(buf) < INPUT_LEN:
        raise BadPacket("not an input packet")
    for_frame, apply_at, sx, sy, cx, cy, buttons = struct.unpack_from(INPUT_FMT, buf, HEADER_LEN)
    return InputPacket(h, for_frame, apply_at, Inputs(sx, sy, cx, cy, buttons))


# --- Ack (kind 4, Switch to PC) ---------------------------------------------
# for_frame, applied_frame, ready_frame
ACK_FMT = "<III"
ACK_LEN = HEADER_LEN + struct.calcsize(ACK_FMT)  # 24


@dataclass(frozen=True)
class Ack:
    header: Header
    for_frame: int
    applied_frame: int
    ready_frame: int

    @property
    def delay(self) -> int:
        """Frames from the state it answered to when it took effect."""
        return (self.applied_frame - self.for_frame) & 0xFFFFFFFF

    @property
    def round_trip(self) -> int:
        """Frames from the state it answered to the first frame it could have
        made. This is the number that sets the training delay."""
        return (self.ready_frame - self.for_frame) & 0xFFFFFFFF


def pack_ack(seq: int, for_frame: int, applied_frame: int, ready_frame: int) -> bytes:
    return pack_header(KIND_ACK, seq) + struct.pack(
        ACK_FMT, for_frame & 0xFFFFFFFF, applied_frame & 0xFFFFFFFF, ready_frame & 0xFFFFFFFF,
    )


def unpack_ack(buf: bytes) -> Ack:
    h = unpack_header(buf)
    if h.kind != KIND_ACK or len(buf) < ACK_LEN:
        raise BadPacket("not an ack")
    return Ack(h, *struct.unpack_from(ACK_FMT, buf, HEADER_LEN))


# --- Control status (kind 5, Switch to PC, with every heartbeat) -------------
# bot_port, bot_player_idx, flags, other_player_idx (v3, was reserved), then 10
# u32 counters, then the game's own mapped inputs for the bot's controller
# (4 x i8 sticks, u32 buttons)
CONTROL_STATUS_FMT = "<BbBb10I4bI"
CONTROL_STATUS_LEN = HEADER_LEN + struct.calcsize(CONTROL_STATUS_FMT)  # 64

FLAG_HOOK_INSTALLED = 1
FLAG_SESSION_ACTIVE = 2
FLAG_RECEIVING = 4
FLAG_CPU_PROBE = 8
FLAG_FIM_OVERRIDE = 16  # fim_player_idx is set in config.toml


@dataclass(frozen=True)
class ControlStatus:
    header: Header
    bot_port: int
    bot_player_idx: int
    flags: int
    fim_calls: int = 0
    idx_seen: int = 0  # bitmask of final input mapping player_idx values seen
    inputs_rx: int = 0
    applied: int = 0
    late: int = 0
    rejected: int = 0
    repeats: int = 0
    neutral: int = 0
    cpu_calls: int = 0
    cpu_idx_seen: int = 0  # bitmask of set_cpu_controls controller_index values seen
    orig: Inputs = Inputs()
    # v3: player_idx the plugin captures as the port that isn't the bot
    other_player_idx: int = 0

    @property
    def fim_override(self) -> bool:
        return bool(self.flags & FLAG_FIM_OVERRIDE)

    def player_idx_by_port(self) -> list[int]:
        """Final input mapping player_idx captured as [port 1, port 2]."""
        out = [0, 1]
        if self.bot_port in (1, 2):
            out[self.bot_port - 1] = self.bot_player_idx
            out[2 - self.bot_port] = self.other_player_idx
        return out

    @property
    def hook_installed(self) -> bool:
        return bool(self.flags & FLAG_HOOK_INSTALLED)

    @property
    def session_active(self) -> bool:
        return bool(self.flags & FLAG_SESSION_ACTIVE)

    @property
    def receiving(self) -> bool:
        return bool(self.flags & FLAG_RECEIVING)

    @property
    def cpu_probe(self) -> bool:
        return bool(self.flags & FLAG_CPU_PROBE)


def bits_list(mask: int) -> list[int | str]:
    """Decode an idx_seen mask. Bit 31 means "some value outside 0..30"."""
    out: list[int | str] = [i for i in range(31) if mask >> i & 1]
    if mask >> 31 & 1:
        out.append("other")
    return out


def pack_control_status(seq: int, s: ControlStatus) -> bytes:
    o = s.orig
    return pack_header(KIND_CONTROL_STATUS, seq) + struct.pack(
        CONTROL_STATUS_FMT, s.bot_port, s.bot_player_idx, s.flags, s.other_player_idx,
        s.fim_calls, s.idx_seen, s.inputs_rx, s.applied, s.late, s.rejected, s.repeats,
        s.neutral, s.cpu_calls, s.cpu_idx_seen,
        o.stick_x, o.stick_y, o.cstick_x, o.cstick_y, o.buttons,
    )


def unpack_control_status(buf: bytes) -> ControlStatus:
    h = unpack_header(buf)
    if h.kind != KIND_CONTROL_STATUS or len(buf) < CONTROL_STATUS_LEN:
        raise BadPacket("not a control status packet")
    v = struct.unpack_from(CONTROL_STATUS_FMT, buf, HEADER_LEN)
    return ControlStatus(h, *v[:3], *v[4:14], orig=Inputs(*v[14:]), other_player_idx=v[3])


def unpack_heartbeat(buf: bytes) -> Heartbeat:
    h = unpack_header(buf)
    if h.kind != KIND_HEARTBEAT or len(buf) < HEARTBEAT_LEN:
        raise BadPacket("not a heartbeat")
    uptime_ms, ver, hooked, calls, queued = struct.unpack_from(HEARTBEAT_FMT, buf, HEADER_LEN)
    return Heartbeat(h, uptime_ms, ver.rstrip(b"\0").decode(errors="replace"), bool(hooked), calls, queued)


# Log lines (kind 6): header + id (u32) + len (u16) + UTF-8 text.
LOG_FMT = "<IH"


@dataclass(frozen=True)
class LogLine:
    header: Header
    id: int
    text: str


def pack_log(seq: int, line_id: int, text: str) -> bytes:
    t = text.encode()[:200]
    return pack_header(KIND_LOG, seq) + struct.pack(LOG_FMT, line_id, len(t)) + t


def unpack_log(buf: bytes) -> LogLine:
    h = unpack_header(buf)
    n0 = HEADER_LEN + struct.calcsize(LOG_FMT)
    if h.kind != KIND_LOG or len(buf) < n0:
        raise BadPacket("not a log packet")
    line_id, n = struct.unpack_from(LOG_FMT, buf, HEADER_LEN)
    return LogLine(h, line_id, buf[n0:n0 + n].decode(errors="replace"))


class LogDedup:
    """The plugin resends its recent lines every few seconds; print each once.
    Ids restart at 0 when the plugin restarts: call reset() then (the
    heartbeat's uptime going backwards)."""

    def __init__(self) -> None:
        self.seen: set[int] = set()

    def reset(self) -> None:
        self.seen.clear()

    def new(self, line: LogLine) -> bool:
        if line.id in self.seen:
            return False
        self.seen.add(line.id)
        return True
