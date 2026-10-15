"""Listen for packets from the Switch plugin and report what arrives.

    python -m bridge.udp_listen --port 4243 --expect-version 13.0.4

Prints the Switch's address on the first packet, then a status line every
5 seconds: packets received, sequence gaps, and the sender's uptime.
"""
from __future__ import annotations

import argparse
import socket
import time

from .wire import KIND_HEARTBEAT, BadPacket, unpack_header, unpack_heartbeat


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


def listen(port: int, expect_version: str | None, report_every: float = 5.0) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(1.0)
    print(f"[listen] waiting for the Switch on UDP :{port}")

    tracker = SeqTracker()
    sender = None
    last_uptime = 0
    last_report = time.monotonic()
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
            tracker.see(header.seq)
            if header.kind == KIND_HEARTBEAT:
                hb = unpack_heartbeat(buf)
                if last_uptime and hb.uptime_ms < last_uptime:
                    print("[listen] uptime went backwards: the plugin restarted")
                    tracker = SeqTracker()
                    tracker.see(header.seq)
                last_uptime = hb.uptime_ms
                if expect_version and hb.game_version != expect_version:
                    print(f"[listen] WARNING: plugin built for {hb.game_version}, expected {expect_version}")

        now = time.monotonic()
        if now - last_report >= report_every:
            last_report = now
            if tracker.received:
                print(
                    f"[listen] got {tracker.received}, missing {tracker.missing} "
                    f"({tracker.loss_pct:.2f}%), out of order {tracker.out_of_order}, "
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
