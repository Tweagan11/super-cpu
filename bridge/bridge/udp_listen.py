"""Listen for packets from the Switch plugin and report what arrives.

    python -m bridge.udp_listen --port 4243 --expect-version 13.0.4
    python -m bridge.udp_listen --log --stage battlefield   # also write each match to data/matches/

Prints the Switch's address on the first packet, the live fighter state once
a second during a match, and a status line every 5 seconds. Match start/end
detection lives in matches.py so the logger and eval share it.
"""
from __future__ import annotations

import argparse
import socket
import time

from .kinds import kind_name
from .matches import FrameTracker, MatchEnded, MatchStarted, MatchWatcher  # noqa: F401 (FrameTracker re-exported)
from .stages import stage_layout, stage_name
from .statuses import status_name
from .protocol import Fighter, Frame
from .wire import KIND_CONTROL_STATUS, BadPacket, Heartbeat, bits_list, unpack_control_status


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


def describe(port: int, f: Fighter) -> str:
    where = "air" if f.airborne else "ground"
    stocks = "?" if f.stocks < 0 else str(f.stocks)
    marks = ("HITSTUN " if f.in_hitstun else "") + ("INVINC " if f.invincible else "")
    return (
        f"P{port} {kind_name(f.fighter_kind):<10} {f.damage:6.1f}%  "
        f"({f.pos_x:7.1f}, {f.pos_y:7.1f}) v({f.vel_x:+5.2f}, {f.vel_y:+5.2f})  "
        f"{stocks} stk  sh {f.shield_hp:4.1f}  j{f.jumps_left}  {where:<6} "
        f"{marks}{status_name(f.status_kind)}"
    )


def stage_line(stage_id: int) -> str:
    layout = stage_layout(stage_id)
    return f"[listen] stage {stage_name(stage_id)} (id {stage_id}, layout {layout or 'other'})"


def winner_line(final) -> str:
    a, b = final.bot, final.opponent
    if a.stocks > b.stocks:
        result = f"P1 {kind_name(a.fighter_kind)} wins"
    elif b.stocks > a.stocks:
        result = f"P2 {kind_name(b.fighter_kind)} wins"
    else:
        result = "no winner by stocks (time out or ended early)"
    return (
        f"[listen] match ended after {final.frame + 1} frames: {result}. "
        f"Final stocks {a.stocks}-{b.stocks}, damage {a.damage:.1f}%-{b.damage:.1f}%"
    )


class ListenPrinter:
    """MatchWatcher observer that prints match start and end lines."""

    def on_match_start(self, ev: MatchStarted) -> None:
        print("[listen] match started")
        print(stage_line(ev.first.stage_id))

    def on_match_end(self, ev: MatchEnded) -> None:
        print(winner_line(ev.final))


def make_watcher(observers: list | None = None) -> MatchWatcher:
    """The listener's watcher: prints match lines, then any extra observers (e.g. MatchLogger)."""
    return MatchWatcher([ListenPrinter(), *(observers or [])])


def listen(
    port: int, expect_version: str | None, report_every: float = 5.0,
    watcher: MatchWatcher | None = None, stop_after: float | None = None,
) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.2)
    print(f"[listen] waiting for the Switch on UDP :{port}")

    watcher = watcher or make_watcher()
    frames = watcher.boundary.frames
    seqs = SeqTracker()
    sender = None
    last_uptime = 0
    started = last_report = last_live = time.monotonic()
    latest = None
    health = None
    control = None
    bad = 0
    bad_reasons: set[str] = set()

    while stop_after is None or time.monotonic() - started < stop_after:
        try:
            buf, addr = sock.recvfrom(2048)
        except socket.timeout:
            buf = None
        if buf is not None:
            try:
                header, msg = watcher.packet(buf, time.monotonic())
            except BadPacket as e:
                bad += 1
                if str(e) not in bad_reasons:
                    bad_reasons.add(str(e))
                    hint = " (rebuild and recopy the plugin)" if "protocol" in str(e) else ""
                    print(f"[listen] ignoring packets from {addr[0]}: {e}{hint}")
                continue
            if sender != addr:
                sender = addr
                print(f"[listen] Switch found at {addr[0]}:{addr[1]}")
            seqs.see(header.seq)
            if isinstance(msg, Heartbeat):
                hb = msg
                if last_uptime and hb.uptime_ms < last_uptime:
                    print("[listen] uptime went backwards: the plugin restarted")
                    seqs = SeqTracker()
                    seqs.see(header.seq)
                last_uptime = hb.uptime_ms
                health = hb
                if expect_version and hb.game_version != expect_version:
                    print(
                        f"[listen] WARNING: Switch is running game {hb.game_version or '?'}, "
                        f"expected {expect_version}. The plugin sends no state on a mismatch."
                    )
            elif isinstance(msg, Frame):
                latest = msg
            elif header.kind == KIND_CONTROL_STATUS:
                try:
                    control = unpack_control_status(buf)
                except BadPacket:
                    bad += 1

        now = time.monotonic()
        # The plugin stops sending state on the results screen.
        watcher.poll(now)
        if latest is not None and now - last_live >= 1.0:
            last_live = now
            print(f"[frame {latest.frame:>6}]  {describe(1, latest.bot)}")
            print(f"{'':16}{describe(2, latest.opponent)}")
            latest = None
        if now - last_report >= report_every:
            fps = frames.window_frames / (now - last_report)
            frames.window_frames = 0
            last_report = now
            if health is not None:
                hook = "installed" if health.hook_installed else "NOT installed"
                print(
                    f"[listen] hook {hook}, fighter frames seen {health.fighter_calls}, "
                    f"frames queued {health.frames_queued}"
                )
            if control is not None:
                o = control.orig
                print(
                    f"[listen] bot port {control.bot_port}, input hook "
                    f"{'installed' if control.hook_installed else 'NOT installed'}, "
                    f"PC session {'on' if control.session_active else 'off'}, "
                    f"input player_idx seen {bits_list(control.idx_seen)}, "
                    f"bot controller stick ({o.stick_x}, {o.stick_y}) buttons {o.buttons:#x}"
                )
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
    ap.add_argument("--log", nargs="?", const="", default=None, metavar="DIR",
                    help="write each match through MatchLogger (default dir data/matches)")
    ap.add_argument("--stage", default="fd", help="fallback layout when the stage id is not Battlefield or FD/Omega: battlefield, fd, omega")
    ap.add_argument("--mod-hash", default="unset")
    ap.add_argument("--p1", default="unknown", help="who's on port 1: a name, bot:<model>, or cpu:9")
    ap.add_argument("--p2", default="unknown", help="who's on port 2")
    ap.add_argument("--seconds", type=float, default=None, help="stop after this long")
    args = ap.parse_args()
    observers = []
    if args.log is not None:
        from .logger import MatchLogger
        from .stages import layout_by_name

        layout_by_name(args.stage)  # fail early on a typo
        observers.append(MatchLogger(
            out_dir=args.log or None, expect_version=args.expect_version, mod_hash=args.mod_hash,
            stage=args.stage, controllers=(args.p1, args.p2),
        ))
    watcher = make_watcher(observers)
    try:
        listen(args.port, args.expect_version, watcher=watcher, stop_after=args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        watcher.close(time.monotonic())  # a match cut off by Ctrl-C still gets logged, marked incomplete


if __name__ == "__main__":
    main()
