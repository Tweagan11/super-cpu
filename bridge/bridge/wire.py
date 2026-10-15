"""UDP packet header and heartbeat, shared by every packet kind.

Must match plugin/src/net.rs and docs/protocol.md.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"SCPU"
PROTO_VERSION = 1

KIND_STATE = 1
KIND_INPUT = 2
KIND_HEARTBEAT = 3

HEADER_FMT = "<4sHHI"  # magic, proto_version, kind, seq
HEADER_LEN = struct.calcsize(HEADER_FMT)  # 12

HEARTBEAT_FMT = "<Q8s"  # uptime_ms, game version (NUL padded)
HEARTBEAT_LEN = HEADER_LEN + struct.calcsize(HEARTBEAT_FMT)  # 28


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


def pack_heartbeat(seq: int, uptime_ms: int, game_version: str) -> bytes:
    return pack_header(KIND_HEARTBEAT, seq) + struct.pack(
        HEARTBEAT_FMT, uptime_ms, game_version.encode()[:8]
    )


def unpack_heartbeat(buf: bytes) -> Heartbeat:
    h = unpack_header(buf)
    if h.kind != KIND_HEARTBEAT or len(buf) < HEARTBEAT_LEN:
        raise BadPacket("not a heartbeat")
    uptime_ms, ver = struct.unpack_from(HEARTBEAT_FMT, buf, HEADER_LEN)
    return Heartbeat(h, uptime_ms, ver.rstrip(b"\0").decode(errors="replace"))
