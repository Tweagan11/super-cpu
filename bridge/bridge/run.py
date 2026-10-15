"""The live bridge (SSB-5): answer every state packet with an input packet.

    python -m bridge.run --policy walk --delay 2 --bot-port 2

Receives state like `bridge.udp_listen` does, runs the policy on the recent
history, and replies to wherever the state came from (the plugin receives on
the same socket it sends from) with `apply_at = frame + delay`. Prints a
status line every few seconds.

`--bot-port` must match `bot_port` in the plugin's config.toml. The plugin
reports its port with every heartbeat and this warns if they differ.

Only one program can own UDP 4243 at a time, so stop udp_listen first.
"""
from __future__ import annotations

import argparse
import socket
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from .policy import POLICIES, History, Policy
from .protocol import Frame
from .udp_listen import FrameTracker, SeqTracker
from .wire import (
    KIND_ACK,
    KIND_CONTROL_STATUS,
    KIND_HEARTBEAT,
    KIND_STATE,
    Ack,
    BadPacket,
    ControlStatus,
    bits_list,
    pack_input,
    unpack_ack,
    unpack_control_status,
    unpack_header,
    unpack_heartbeat,
    unpack_state,
)

# The plugin keeps 32 frames of pending input; anything further ahead is refused.
MAX_DELAY = 31


def percentile(values: list[int], pct: float) -> int:
    """Nearest-rank percentile, so the answer is always a frame count that happened."""
    if not values:
        raise ValueError("no values")
    s = sorted(values)
    rank = max(1, -(-len(s) * pct // 100))  # ceil
    return s[int(rank) - 1]


@dataclass
class AckStats:
    """Round trip numbers from the plugin's acks."""

    round_trips: list[int] = field(default_factory=list)  # ready_frame - for_frame
    delays: list[int] = field(default_factory=list)  # applied_frame - for_frame
    seen: set[int] = field(default_factory=set)

    def add(self, a: Ack) -> None:
        if a.for_frame in self.seen:
            return
        self.seen.add(a.for_frame)
        self.round_trips.append(a.round_trip)
        self.delays.append(a.delay)

    def __len__(self) -> int:
        return len(self.round_trips)

    def summary(self) -> str:
        if not self.round_trips:
            return "no acks yet"
        return (
            f"round trip median {statistics.median_low(self.round_trips)} "
            f"p99 {percentile(self.round_trips, 99)} max {max(self.round_trips)} frames"
        )


class Bridge:
    """One UDP socket, state in, inputs out. `bridge.latency` drives this too."""

    def __init__(
        self,
        port: int,
        policy: Policy,
        delay: int,
        bot_port: int,
        expect_version: str | None = None,
        bind: str = "0.0.0.0",
        log: Callable[[str], None] = print,
    ) -> None:
        if not 0 <= delay <= MAX_DELAY:
            raise ValueError(f"delay must be 0 to {MAX_DELAY} frames")
        if bot_port not in (1, 2):
            raise ValueError("bot port must be 1 or 2 (state only carries ports 1 and 2)")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((bind, port))
        self.policy = policy
        self.delay = delay
        self.bot_port = bot_port
        self.expect_version = expect_version
        self.log = log

        self.switch: tuple[str, int] | None = None
        self.seqs = SeqTracker()
        self.frames = FrameTracker()
        self.history = History()
        self.acks = AckStats()
        self.out_seq = 0
        self.inputs_sent = 0
        # for_frame of every input sent; only bridge.latency turns this on
        self.sent_frames: list[int] | None = None
        self.last_uptime = 0
        self.status: ControlStatus | None = None
        self.first_status: ControlStatus | None = None
        self.bad = 0
        self.bad_reasons: set[str] = set()
        self.kinds: Counter[int] = Counter()
        self._warned_port = False
        self._warned_hook = False
        self.on_ack: Callable[[Ack], None] | None = None

    @property
    def port(self) -> int:
        return self.sock.getsockname()[1]

    def close(self) -> None:
        self.sock.close()

    def poll(self, timeout: float) -> int | None:
        """Handle at most one packet. Returns its kind, or None on timeout/bad."""
        self.sock.settimeout(timeout)
        try:
            buf, addr = self.sock.recvfrom(2048)
        except (socket.timeout, BlockingIOError):
            return None
        except ConnectionResetError:
            # Windows reports an ICMP "port unreachable" from an earlier send here.
            return None
        try:
            header = unpack_header(buf)
        except BadPacket as e:
            self.bad += 1
            if str(e) not in self.bad_reasons:
                self.bad_reasons.add(str(e))
                hint = " (rebuild and recopy the plugin)" if "protocol" in str(e) else ""
                self.log(f"[run] ignoring packets from {addr[0]}: {e}{hint}")
            return None
        if self.switch != addr:
            self.switch = addr
            self.log(f"[run] Switch found at {addr[0]}:{addr[1]}, replying there")
        self.seqs.see(header.seq)
        self.kinds[header.kind] += 1
        try:
            if header.kind == KIND_STATE:
                self._on_state(buf, addr)
            elif header.kind == KIND_ACK:
                a = unpack_ack(buf)
                self.acks.add(a)
                if self.on_ack:
                    self.on_ack(a)
            elif header.kind == KIND_CONTROL_STATUS:
                self._on_status(unpack_control_status(buf))
            elif header.kind == KIND_HEARTBEAT:
                hb = unpack_heartbeat(buf)
                if self.last_uptime and hb.uptime_ms < self.last_uptime:
                    self.log("[run] uptime went backwards: the plugin restarted")
                    self.seqs = SeqTracker()
                    self.seqs.see(header.seq)
                self.last_uptime = hb.uptime_ms
                if self.expect_version and hb.game_version != self.expect_version:
                    self.log(f"[run] WARNING: plugin built for {hb.game_version}, expected {self.expect_version}")
                    self.expect_version = None
        except BadPacket as e:
            self.bad += 1
            if str(e) not in self.bad_reasons:
                self.bad_reasons.add(str(e))
                self.log(f"[run] bad packet: {e}")
            return None
        return header.kind

    def _on_state(self, buf: bytes, addr: tuple[str, int]) -> None:
        _, frame = unpack_state(buf, self.bot_port - 1)
        if self.frames.see(frame.frame):
            self.history = History()
            self.log("[run] match started")
        inputs = self.policy.act(self.history.push(frame))
        self.send_input(frame, inputs, addr)

    def send_input(self, frame: Frame, inputs, addr: tuple[str, int]) -> None:
        packet = pack_input(self.out_seq, frame.frame, frame.frame + self.delay, inputs)
        self.sock.sendto(packet, addr)
        self.out_seq = (self.out_seq + 1) & 0xFFFFFFFF
        self.inputs_sent += 1
        if self.sent_frames is not None:
            self.sent_frames.append(frame.frame)

    def _on_status(self, s: ControlStatus) -> None:
        first = self.status is None
        if first:
            self.first_status = s
        self.status = s
        if s.bot_port != self.bot_port and not self._warned_port:
            self._warned_port = True
            self.log(
                f"[run] WARNING: the plugin drives port {s.bot_port} but --bot-port is {self.bot_port}. "
                f"Set bot_port in config.toml or pass --bot-port {s.bot_port}."
            )
        if not s.hook_installed and not self._warned_hook:
            self._warned_hook = True
            self.log("[run] WARNING: the plugin's input hook is NOT installed, the bot can't move")
        if first:
            self.log(
                f"[run] plugin: bot port {s.bot_port} (input player_idx {s.bot_player_idx}), "
                f"input hook {'installed' if s.hook_installed else 'NOT installed'}, "
                f"receiving {'yes' if s.receiving else 'NO'}"
            )

    def status_line(self, fps: float) -> str:
        parts = [f"frames {fps:.1f}/s", f"inputs sent {self.inputs_sent}", f"acks {len(self.acks)}"]
        if self.acks.round_trips:
            parts.append(
                f"rt med {statistics.median_low(self.acks.round_trips)} p99 {percentile(self.acks.round_trips, 99)}"
            )
        s = self.status
        if s is not None:
            parts.append(
                f"switch: rx {s.inputs_rx} applied {s.applied} late {s.late} repeat {s.repeats} neutral {s.neutral} "
                f"rejected {s.rejected}, session {'on' if s.session_active else 'off'}"
            )
        parts.append(f"lost {self.seqs.loss_pct:.1f}%")
        return "[run] " + ", ".join(parts)

    def hardware_notes(self) -> str | None:
        """What the open questions in docs/hooks.md look like so far."""
        s = self.status
        if s is None:
            return None
        cpu = f", cpu controller_index seen {bits_list(s.cpu_idx_seen)}" if s.cpu_probe else ""
        o = s.orig
        return (
            f"[run] hooks: input player_idx seen {bits_list(s.idx_seen)}{cpu}; "
            f"game's own input for the bot's controller: stick ({o.stick_x}, {o.stick_y}) "
            f"c ({o.cstick_x}, {o.cstick_y}) buttons {o.buttons:#x}"
        )


def run(port: int, policy: Policy, delay: int, bot_port: int, expect_version: str | None,
        report_every: float = 5.0, seconds: float | None = None) -> Bridge:
    b = Bridge(port, policy, delay, bot_port, expect_version)
    print(
        f"[run] {type(policy).__name__} on port {bot_port}, delay {delay} frames, "
        f"waiting for the Switch on UDP :{b.port}"
    )
    start = last_report = time.monotonic()
    last_frames = 0
    reports = 0
    try:
        while seconds is None or time.monotonic() - start < seconds:
            b.poll(0.1)
            now = time.monotonic()
            if now - last_report >= report_every:
                fps = (b.frames.frames - last_frames) / (now - last_report)
                last_frames = b.frames.frames
                last_report = now
                reports += 1
                if b.seqs.received:
                    print(b.status_line(fps))
                    if reports % 6 == 1 and (notes := b.hardware_notes()):
                        print(notes)
                else:
                    print("[run] nothing yet. Check the IP in the plugin config and your firewall.")
    except KeyboardInterrupt:
        print("\n[run] stopped. The plugin hands the bot port back within 2 s.")
    finally:
        b.close()
    return b


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=4243)
    ap.add_argument("--policy", choices=sorted(POLICIES), default="walk")
    ap.add_argument("--delay", type=int, default=2, help="apply_at = state frame + this (frames)")
    ap.add_argument("--bot-port", type=int, default=2, choices=(1, 2), help="must match bot_port in config.toml")
    ap.add_argument("--expect-version", default="13.0.4")
    ap.add_argument("--report-every", type=float, default=5.0)
    ap.add_argument("--seconds", type=float, default=None, help="stop after this long")
    args = ap.parse_args()
    run(args.port, POLICIES[args.policy](), args.delay, args.bot_port, args.expect_version,
        args.report_every, args.seconds)


if __name__ == "__main__":
    main()
