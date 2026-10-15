"""Listen for packets from the Switch plugin and report what arrives.

    python -m bridge.udp_listen --port 4243 --expect-version 13.0.4

Prints the Switch's address on the first packet, the live fighter state once
a second during a match, and a status line every 5 seconds.
"""
from __future__ import annotations

import argparse
import socket
import time

from .kinds import kind_name
from .protocol import Fighter
from .wire import KIND_HEARTBEAT, KIND_STATE, BadPacket, unpack_header, unpack_heartbeat, unpack_state


class SeqTracker:
    """Counts received packets and gaps in the sequence numbers."""

    def __init__(self) -> None:
        self.last: int | None = None
        self.received = 0
        self.missing = 0
        self.out_of_order = 0

    def see(self, seq: int) -> None:
        self.received += 1
        if self.last is None:
            self.last = seq
            return
        step = (seq - self.last) & 0xFFFFFFFF
        if step == 0 or step > 0x7FFFFFFF:
            self.out_of_order += 1
            return
        self.missing += step - 1
        self.last = seq

    @property
    def loss_pct(self) -> float:
        total = self.received + self.missing
        return 100.0 * self.missing / total if total else 0.0


class FrameTracker:
    """Counts game frames received, frames skipped, and frames per second."""

    def __init__(self) -> None:
        self.last_frame: int | None = None
        self.frames = 0
        self.skipped = 0
        self.window_frames = 0

    def see(self, frame: int) -> bool:
        """Returns True when this frame starts a new match."""
        new_match = self.last_frame is None or frame < self.last_frame
        if not new_match and frame > self.last_frame + 1:
            self.skipped += frame - self.last_frame - 1
        self.last_frame = frame
        self.frames += 1
        self.window_frames += 1
        return new_match


def describe(port: int, f: Fighter) -> str:
    where = "air" if f.airborne else "ground"
    stocks = "?" if f.stocks < 0 else str(f.stocks)
    return (
        f"P{port} {kind_name(f.fighter_kind):<10} {f.damage:6.1f}%  "
        f"({f.pos_x:7.1f}, {f.pos_y:7.1f})  {stocks} stk  {where:<6} "
        f"status {f.status_kind}"
    )


def listen(port: int, expect_version: str | None, report_every: float = 5.0) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.2)
    print(f"[listen] waiting for the Switch on UDP :{port}")

    seqs = SeqTracker()
    frames = FrameTracker()
    sender = None
    last_uptime = 0
    last_report = last_live = time.monotonic()
    latest = None
    bad = 0

    while True:
        try:
            buf, addr = sock.recvfrom(2048)
        except socket.timeout:
            buf = None
        if buf is not None:
            try:
                header = unpack_header(buf)
            except BadPacket as e:
                bad += 1
                print(f"[listen] ignored packet from {addr[0]}: {e}")
                continue
            if sender != addr:
                sender = addr
                print(f"[listen] Switch found at {addr[0]}:{addr[1]}")
            seqs.see(header.seq)
            if header.kind == KIND_HEARTBEAT:
                hb = unpack_heartbeat(buf)
                if last_uptime and hb.uptime_ms < last_uptime:
                    print("[listen] uptime went backwards: the plugin restarted")
                    seqs = SeqTracker()
                    seqs.see(header.seq)
                last_uptime = hb.uptime_ms
                if expect_version and hb.game_version != expect_version:
                    print(f"[listen] WARNING: plugin built for {hb.game_version}, expected {expect_version}")
            elif header.kind == KIND_STATE:
                _, latest = unpack_state(buf)
                if frames.see(latest.frame):
                    print("[listen] match started")

        now = time.monotonic()
        if latest is not None and now - last_live >= 1.0:
            last_live = now
            print(f"[frame {latest.frame:>6}]  {describe(1, latest.bot)}")
            print(f"{'':16}{describe(2, latest.opponent)}")
            latest = None
        if now - last_report >= report_every:
            fps = frames.window_frames / (now - last_report)
            frames.window_frames = 0
            last_report = now
            if seqs.received:
                print(
                    f"[listen] packets {seqs.received}, lost {seqs.missing} ({seqs.loss_pct:.2f}%), "
                    f"frames {frames.frames} at {fps:.1f}/s, skipped {frames.skipped}, "
                    f"bad {bad}, switch uptime {last_uptime / 1000:.1f}s"
                )
            else:
                print("[listen] nothing yet. Check the IP in the plugin config and your firewall.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4243)
    ap.add_argument("--expect-version", default="13.0.4")
    args = ap.parse_args()
    listen(args.port, args.expect_version)


if __name__ == "__main__":
    main()
