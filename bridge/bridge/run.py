"""The live bridge (SSB-5): answer every state packet with an input packet.

    python -m bridge.run --policy walk --delay 2 --bot-port 2
    python -m bridge.run --policy scripted --log                     # also log every match
    python -m bridge.run --policy scripted --log --eval 50 --model scripted --stage fd

Match start/end comes from a matches.MatchWatcher, so logging (`--log`,
MatchLogger) and scoring (`--eval N`, EvalSession) run in this same process
on the same socket. With `--eval N` it stops after N counted games and writes
one row to eval/results.csv. The bot's own inputs are logged on the frame
they took effect (from the plugin's acks), and the plugin's captured inputs
for both ports fill in the rest (docs/logging-schema.md).

Receives state like `bridge.udp_listen` does, runs the policy on the recent
history, and replies to wherever the state came from (the plugin receives on
the same socket it sends from) with `apply_at = frame + delay`. Prints a
status line every few seconds.

`--bot-port` must match `bot_port` in the plugin's config.toml. The plugin
reports its port with every heartbeat and this warns if they differ.

`--auto-rematch` (SSB-25) keeps matches going unattended: once a match has
ended and the plugin reports the results scene it presses A (menu press
packets, kind 8) until the scene changes, then + on the CSS to start the next
one, with a growing wait between presses and a cap per scene (rematch.py).
It needs `auto_menu = true` in the plugin's config.toml. The scene names are
guesses until hardware experiment E1: this logs every scene it sees, and
`--css-scene` / `--results-scene` / `--match-scene` (substrings, repeatable)
override them.

Only one program can own UDP 4243 at a time, so stop udp_listen first.
"""
from __future__ import annotations

import argparse
import socket
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .matches import MatchStarted, MatchWatcher
from .policy import POLICIES, History, Policy
from .protocol import Frame, Inputs
from .rematch import Rematch
from .scenes import DEFAULT_NAMES, SceneNames
from .udp_listen import SeqTracker
from .wire import (
    KIND_ACK,
    KIND_CONTROL_STATUS,
    KIND_HEARTBEAT,
    KIND_LOG,
    KIND_SCENE,
    KIND_STATE,
    Ack,
    BadPacket,
    ControlStatus,
    LogDedup,
    bits_list,
    pack_input,
    pack_menu,
    unpack_ack,
    unpack_control_status,
    unpack_header,
    unpack_heartbeat,
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


class BotInputRecorder:
    """Puts the bot's own inputs in the match log on the frame they took effect.

    Remembers what was sent for each state frame; when the plugin acks
    (for_frame, applied_frame), the input goes in the log at applied_frame.
    Repeats and neutral frames aren't acked, so this alone leaves them
    invalid; the plugin's per-frame capture (state packet v3) fills those in
    with what was really applied.
    """

    KEEP = 256  # frames of sent inputs to remember (the plugin's ring is 32)

    def __init__(self, logger, bot_port: int) -> None:
        self.logger = logger
        self.bot_port = bot_port
        self.sent: dict[int, Inputs] = {}
        self.recorded = 0

    def on_match_start(self, ev: MatchStarted) -> None:
        self.sent.clear()  # an ack from the last match can't land in this one

    def on_sent(self, for_frame: int, inputs: Inputs) -> None:
        self.sent[for_frame] = inputs
        if len(self.sent) > self.KEEP:
            self.sent.pop(next(iter(self.sent)))

    def on_ack(self, a: Ack) -> None:
        inputs = self.sent.get(a.for_frame)
        if inputs is None or not self.logger.in_match:
            return
        self.logger.record_inputs(self.bot_port, a.applied_frame, inputs)
        self.recorded += 1


class _MatchHooks:
    """Watcher observer that resets the policy history on a new match."""

    def __init__(self, bridge: "Bridge") -> None:
        self.bridge = bridge

    def on_match_start(self, ev: MatchStarted) -> None:
        self.bridge.history = History()
        self.bridge.log("[run] match started")


class Bridge:
    """One UDP socket, state in, inputs out. `bridge.latency` drives this too.

    `observers` go on the MatchWatcher (MatchLogger, EvalSession.observers(),
    anything with on_match_start / on_frame / on_match_end). `logger`, if
    given, also gets the bot's acked inputs.
    """

    def __init__(
        self,
        port: int,
        policy: Policy,
        delay: int,
        bot_port: int,
        expect_version: str | None = None,
        bind: str = "0.0.0.0",
        log: Callable[[str], None] = print,
        observers: Sequence[object] = (),
        logger=None,
        auto_rematch: bool = False,
        scene_names: SceneNames = DEFAULT_NAMES,
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
        self.logs = LogDedup()
        self.history = History()
        self.recorder = BotInputRecorder(logger, bot_port) if logger is not None else None
        hooks: list[object] = [_MatchHooks(self)]
        if self.recorder is not None:
            hooks.append(self.recorder)
        self.rematch = Rematch(self.send_menu, log, scene_names) if auto_rematch else None
        if self.rematch is not None:
            hooks.append(self.rematch)
        self.menu_seq = 0
        self.scene_name: str | None = None
        self._warned_menu = False
        # Learning policies (training/rl.py) want to know the real delay and
        # port, and to hear about match boundaries and shutdown.
        configure = getattr(policy, "configure", None)
        if configure is not None:
            configure(delay=delay, bot_port=bot_port)
        if hasattr(policy, "on_match_start") or hasattr(policy, "on_match_end"):
            hooks.append(policy)
        self.watcher = MatchWatcher([*hooks, *observers], bot_port=bot_port)
        self.frames = self.watcher.boundary.frames
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
        """Ends a match in progress (logged as interrupted) and closes the socket."""
        self.watcher.close(time.monotonic())
        self.sock.close()
        close = getattr(self.policy, "close", None)
        if close is not None:
            close()

    def poll(self, timeout: float) -> int | None:
        """Handle at most one packet. Returns its kind, or None on timeout/bad.
        Also checks for the end of a match, so call it even when idle."""
        kind = self._poll(timeout)
        now = time.monotonic()
        self.watcher.poll(now)
        if self.rematch is not None:
            self.rematch.tick(now)
        return kind

    def _poll(self, timeout: float) -> int | None:
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
                if self.recorder is not None:
                    self.recorder.on_ack(a)
                if self.on_ack:
                    self.on_ack(a)
            elif header.kind == KIND_CONTROL_STATUS:
                _, st = self.watcher.packet(buf, time.monotonic())
                self._on_status(st)
            elif header.kind == KIND_LOG:
                _, line = self.watcher.packet(buf, time.monotonic())
                if self.logs.new(line):
                    self.log(f"[switch] {line.text}")
            elif header.kind == KIND_SCENE:
                _, sc = self.watcher.packet(buf, time.monotonic())
                self._on_scene(sc)
            elif header.kind == KIND_HEARTBEAT:
                _, hb = self.watcher.packet(buf, time.monotonic())
                if self.last_uptime and hb.uptime_ms < self.last_uptime:
                    self.log("[run] uptime went backwards: the plugin restarted")
                    self.seqs = SeqTracker()
                    self.logs.reset()
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
        # The watcher decodes with the bot port, so frame.bot is the bot, and
        # runs match start (resets history) before the policy sees the frame.
        _, frame = self.watcher.packet(buf, time.monotonic())
        inputs = self.policy.act(self.history.push(frame))
        self.send_input(frame, inputs, addr)

    def send_input(self, frame: Frame, inputs, addr: tuple[str, int]) -> None:
        if self.recorder is not None:
            self.recorder.on_sent(frame.frame, inputs)
        packet = pack_input(self.out_seq, frame.frame, frame.frame + self.delay, inputs)
        self.sock.sendto(packet, addr)
        self.out_seq = (self.out_seq + 1) & 0xFFFFFFFF
        self.inputs_sent += 1
        if self.sent_frames is not None:
            self.sent_frames.append(frame.frame)

    def send_menu(self, mask: int, hold_frames: int = 1) -> None:
        """One menu press (kind 8) to the Switch. Needs auto_menu on in the plugin."""
        if self.switch is None:
            return
        packet = pack_menu(self.out_seq, self.menu_seq, mask, hold_frames)
        self.sock.sendto(packet, self.switch)
        self.out_seq = (self.out_seq + 1) & 0xFFFFFFFF
        self.menu_seq = (self.menu_seq + 1) & 0xFFFFFFFF

    def _on_scene(self, sc) -> None:
        if sc.name != self.scene_name:
            self.scene_name = sc.name
            self.log(f"[run] scene: {sc.name!r} (change #{sc.changes}, menu pulses applied {sc.menu_applied})")
        if self.rematch is not None and not sc.menu_hook and not self._warned_menu:
            self._warned_menu = True
            self.log("[run] WARNING: --auto-rematch is on but the plugin's menu hook is off. "
                     "Set auto_menu = true in config.toml, or the presses are ignored.")

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
            f"[run] hooks{' (drive_cpu: bot is a CPU slot)' if s.drive_cpu else ''}: input player_idx seen {bits_list(s.idx_seen)}{cpu}; "
            f"game's own input for the bot's controller: stick ({o.stick_x}, {o.stick_y}) "
            f"c ({o.cstick_x}, {o.cstick_y}) buttons {o.buttons:#x}"
        )


def run(port: int, policy: Policy, delay: int, bot_port: int, expect_version: str | None,
        report_every: float = 5.0, seconds: float | None = None, logger=None, session=None,
        bind: str = "0.0.0.0", idle_timeout: float | None = None,
        auto_rematch: bool = False, scene_names: SceneNames = DEFAULT_NAMES) -> Bridge:
    """`logger` (a MatchLogger) logs every match; `session` (an EvalSession)
    scores them and stops the loop once it has all its games. With a session,
    its own logger is the one used (pass write_logs to it for files)."""
    if session is not None:
        logger = session.logger
        observers = session.observers()
    else:
        observers = [logger] if logger is not None else []
    b = Bridge(port, policy, delay, bot_port, expect_version, bind=bind, observers=observers, logger=logger,
               auto_rematch=auto_rematch, scene_names=scene_names)
    print(
        f"[run] {type(policy).__name__} on port {bot_port}, delay {delay} frames, "
        f"waiting for the Switch on UDP :{b.port}"
        + (f", logging to {logger.out_dir}" if logger is not None and logger.write else "")
        + (f", eval {session.games} games" if session is not None else "")
        + (", auto rematch" if auto_rematch else "")
    )
    start = last_report = last_packet = time.monotonic()
    last_frames = 0
    reports = 0
    try:
        while seconds is None or time.monotonic() - start < seconds:
            if b.poll(0.1) is not None:
                last_packet = time.monotonic()
            if session is not None and session.done:
                break
            now = time.monotonic()
            if idle_timeout and now - last_packet > idle_timeout:
                print(f"[run] nothing from the Switch for {idle_timeout:.0f}s, stopping")
                break
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
        b.close()  # a match cut off here is logged as interrupted
    if session is not None and not session.done:
        print(f"[run] eval stopped at {len(session.rows)}/{session.games} games, no results.csv row written. "
              f"Per-match detail is in {session.match_csv}")
    return b


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=4243)
    ap.add_argument("--policy", choices=sorted(POLICIES), default="walk")
    ap.add_argument("--delay", type=int, default=3, help="apply_at = state frame + this (frames). 3 = measured worst case on Wi-Fi (SSB-7)")
    ap.add_argument("--bot-port", type=int, default=2, choices=(1, 2), help="must match bot_port in config.toml")
    ap.add_argument("--expect-version", default="13.0.4")
    ap.add_argument("--report-every", type=float, default=5.0)
    ap.add_argument("--seconds", type=float, default=None, help="stop after this long")
    ap.add_argument("--idle-timeout", type=float, default=None, help="stop after this long with no packets")
    ap.add_argument("--log", nargs="?", const="", default=None, metavar="DIR",
                    help="write each match through MatchLogger (default <repo>/data/matches)")
    ap.add_argument("--eval", type=int, default=None, metavar="N",
                    help="score N games (EvalSession) and add a row to eval/results.csv, then stop")
    ap.add_argument("--model", default=None, help="model name for the logs and eval row (default: the policy)")
    ap.add_argument("--opponent", default="cpu9", help="who's on the other port: cpu9 or a player name")
    ap.add_argument("--controllers", default=None, metavar="P1,P2",
                    help="override who's logged on each port, e.g. cpu:9,cpu:9 for CPU vs CPU data collection")
    ap.add_argument("--stage", default="fd", help="fallback layout when the stage id is not Battlefield or FD/Omega")
    ap.add_argument("--stocks", type=int, default=3, help="stock count set in the game's rules (recorded in the logs)")
    ap.add_argument("--platform", default="switch", choices=("switch", "emulator"))
    ap.add_argument("--mod-hash", default="unset")
    ap.add_argument("--min-seconds", type=float, default=10.0, help="eval: ignore matches shorter than this")
    ap.add_argument("--results", type=Path, default=None, help="eval: results CSV (default eval/results.csv)")
    ap.add_argument("--data-dir", type=Path, default=None, help="eval: per-match CSV dir (default <repo>/data)")
    ap.add_argument("--auto-rematch", action="store_true",
                    help="after a match, press A through results and + on the CSS (needs auto_menu = true on the plugin)")
    ap.add_argument("--css-scene", action="append", metavar="TEXT", help="scene name substring that means the CSS (repeatable)")
    ap.add_argument("--results-scene", action="append", metavar="TEXT", help="scene name substring for the results screen")
    ap.add_argument("--match-scene", action="append", metavar="TEXT", help="scene name substring for the match")
    args = ap.parse_args(argv)
    names = SceneNames(
        css=tuple(args.css_scene or DEFAULT_NAMES.css),
        results=tuple(args.results_scene or DEFAULT_NAMES.results),
        match=tuple(args.match_scene or DEFAULT_NAMES.match),
    )
    from .stages import layout_by_name

    layout_by_name(args.stage)  # fail early on a typo
    model = args.model or args.policy
    logger = session = None
    if args.eval is not None:
        from .eval import RESULTS_CSV, EvalSession

        session = EvalSession(
            games=args.eval, model=model, opponent=args.opponent, bot_port=args.bot_port,
            platform=args.platform, stage=args.stage, mod_hash=args.mod_hash, stocks=args.stocks,
            expect_version=args.expect_version, min_seconds=args.min_seconds,
            results_csv=args.results or RESULTS_CSV, data_dir=args.data_dir,
            write_logs=args.log is not None,
        )
        if args.log:
            session.logger.out_dir = Path(args.log)
    elif args.log is not None:
        from .logger import DEFAULT_RULESET, MatchLogger

        controllers = ["", ""]
        controllers[args.bot_port - 1] = f"bot:{model}"
        controllers[2 - args.bot_port] = "cpu:9" if args.opponent == "cpu9" else args.opponent
        if args.controllers:
            controllers = [c.strip() for c in args.controllers.split(",")]
            if len(controllers) != 2:
                ap.error("--controllers takes two names, e.g. cpu:9,cpu:9")
        logger = MatchLogger(
            out_dir=args.log or None, expect_version=args.expect_version, mod_hash=args.mod_hash,
            stage=args.stage, controllers=tuple(controllers),
            ruleset={**DEFAULT_RULESET, "stocks": args.stocks},
        )
    run(args.port, POLICIES[args.policy](), args.delay, args.bot_port, args.expect_version,
        args.report_every, args.seconds, logger=logger, session=session, idle_timeout=args.idle_timeout,
        auto_rematch=args.auto_rematch, scene_names=names)
    return 0 if session is None or session.done else 1


if __name__ == "__main__":
    raise SystemExit(main())
