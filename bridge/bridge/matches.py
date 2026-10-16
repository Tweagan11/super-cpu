"""Match boundaries, shared by the listener, the logger and the eval harness.

The plugin doesn't send "match started" or "match ended" messages, so we infer
them from the state stream:

- New match: the frame counter goes backwards (it restarts at 0 each match),
  or state starts arriving again after a match ended.
- Match end: state stops for more than 1.5 s. The plugin stops sending on the
  results screen, so this is the normal way a match ends. A frame counter
  reset with no gap also ends the previous match.

`MatchBoundary` is the pure logic (feed it frames and a clock, get events).
`MatchWatcher` wraps it, decodes raw UDP packets, and fans everything out to
observers like `MatchLogger` and the eval recorder, so anything that owns the
socket (udp_listen, eval, bridge.run) can plug them in with a few lines.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

from .protocol import Fighter, Frame
from .wire import (
    KIND_CONTROL_STATUS,
    KIND_HEARTBEAT,
    KIND_LOG,
    KIND_STATE,
    Header,
    Heartbeat,
    unpack_control_status,
    unpack_log,
    unpack_header,
    unpack_heartbeat,
    unpack_state,
)

END_TIMEOUT_S = 1.5

# Why a match ended.
END_STATE_STOPPED = "state_stopped"  # no state for END_TIMEOUT_S (results screen, normal)
END_FRAME_RESET = "frame_reset"      # a new match started with no gap
END_INTERRUPTED = "interrupted"      # the PC side shut down mid-match


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


@dataclass(frozen=True)
class MatchStarted:
    first: Frame
    now: float


@dataclass(frozen=True)
class MatchEnded:
    final: Frame
    reason: str
    now: float


class MatchBoundary:
    """Turns a stream of frames plus a clock into start and end events."""

    def __init__(self, timeout: float = END_TIMEOUT_S) -> None:
        self.timeout = timeout
        self.frames = FrameTracker()
        self.final: Frame | None = None  # last frame of the current match, None between matches
        self.last_state_at = 0.0

    @property
    def in_match(self) -> bool:
        return self.final is not None

    def on_frame(self, frame: Frame, now: float) -> list[MatchStarted | MatchEnded]:
        events: list[MatchStarted | MatchEnded] = []
        if self.frames.see(frame.frame):
            if self.final is not None:
                events.append(MatchEnded(self.final, END_FRAME_RESET, now))
            events.append(MatchStarted(frame, now))
        self.final = frame
        self.last_state_at = now
        return events

    def poll(self, now: float) -> MatchEnded | None:
        """Call regularly, even when nothing arrives. Ends the match on a timeout."""
        if self.final is not None and now - self.last_state_at > self.timeout:
            return self._end(END_STATE_STOPPED, now)
        return None

    def close(self, now: float) -> MatchEnded | None:
        """End the current match because we're shutting down."""
        return self._end(END_INTERRUPTED, now) if self.final is not None else None

    def _end(self, reason: str, now: float) -> MatchEnded:
        ev = MatchEnded(self.final, reason, now)
        self.final = None
        self.frames.last_frame = None
        return ev


def fighters(frame: Frame) -> tuple[Fighter, Fighter]:
    """Fighters by port index (0 = port 1), whichever one is the bot."""
    return frame.by_port


def winner_port(final: Frame) -> int | None:
    """1 or 2 by remaining stocks, None on a tie or unknown stocks."""
    a, b = fighters(final)
    if a.stocks < 0 or b.stocks < 0 or a.stocks == b.stocks:
        return None
    return 1 if a.stocks > b.stocks else 2


@dataclass
class MatchSummary:
    """Running totals for one match, cheap enough to keep for every frame."""

    first: Frame
    final: Frame | None = None
    frame_count: int = 0
    start_stocks: list[int] = field(default_factory=lambda: [-1, -1])
    damage_taken: list[float] = field(default_factory=lambda: [0.0, 0.0])
    stocks_lost: list[int] = field(default_factory=lambda: [0, 0])
    _prev: Frame | None = None

    def __post_init__(self) -> None:
        self.start_stocks = [f.stocks for f in fighters(self.first)]

    def add(self, frame: Frame) -> None:
        self.frame_count += 1
        # On hardware a fighter's stock count reads 0 for the first frames of
        # a match (P2, 2026-10-16: 0 0 0 0 0 then 3). Stocks never go up
        # during a match, so the most seen is the start count.
        for i, f in enumerate(fighters(frame)):
            self.start_stocks[i] = max(self.start_stocks[i], f.stocks)
        if self._prev is not None:
            for i, (old, new) in enumerate(zip(fighters(self._prev), fighters(frame))):
                # Damage resets to 0 on a stock loss, so only count increases.
                if new.damage > old.damage:
                    self.damage_taken[i] += new.damage - old.damage
                if old.stocks > 0 and new.stocks >= 0 and new.stocks < old.stocks:
                    self.stocks_lost[i] += old.stocks - new.stocks
        self._prev = frame
        self.final = frame

    @property
    def winner_port(self) -> int | None:
        return winner_port(self.final) if self.final is not None else None


class MatchObserver(Protocol):
    def on_heartbeat(self, hb: Heartbeat) -> None: ...
    def on_control_status(self, s) -> None: ...
    def on_match_start(self, ev: MatchStarted) -> None: ...
    def on_frame(self, frame: Frame, seq: int, now: float) -> None: ...
    def on_match_end(self, ev: MatchEnded) -> None: ...


class MatchWatcher:
    """MatchBoundary plus fan-out to observers.

    Order per state packet: end of the old match (on a frame reset), start of
    the new one, then the frame itself. Observers can skip methods they don't
    care about by not defining them.

    `bot_port` (1 or 2) is which port is the bot, so `frame.bot` is the bot
    for every observer. Logs and scores are by port either way (`fighters()`).
    """

    def __init__(self, observers: Sequence[object] = (), timeout: float = END_TIMEOUT_S, bot_port: int = 1) -> None:
        if bot_port not in (1, 2):
            raise ValueError("bot port must be 1 or 2")
        self.boundary = MatchBoundary(timeout)
        self.observers = list(observers)
        self.bot_port = bot_port

    def _emit(self, name: str, *args) -> None:
        for obs in self.observers:
            fn = getattr(obs, name, None)
            if fn is not None:
                fn(*args)

    def _dispatch(self, ev: MatchStarted | MatchEnded | None) -> None:
        if isinstance(ev, MatchStarted):
            self._emit("on_match_start", ev)
        elif isinstance(ev, MatchEnded):
            self._emit("on_match_end", ev)

    def heartbeat(self, hb: Heartbeat) -> None:
        self._emit("on_heartbeat", hb)

    def state(self, seq: int, frame: Frame, now: float) -> None:
        for ev in self.boundary.on_frame(frame, now):
            self._dispatch(ev)
        self._emit("on_frame", frame, seq, now)

    def packet(self, buf: bytes, now: float) -> tuple[Header, object]:
        """Decode one raw UDP packet and route it. Raises wire.BadPacket.

        Returns the header and the decoded Heartbeat, Frame or ControlStatus
        (None for other kinds).
        """
        header = unpack_header(buf)
        if header.kind == KIND_HEARTBEAT:
            hb = unpack_heartbeat(buf)
            self.heartbeat(hb)
            return header, hb
        if header.kind == KIND_STATE:
            _, frame = unpack_state(buf, self.bot_port - 1)
            self.state(header.seq, frame, now)
            return header, frame
        if header.kind == KIND_CONTROL_STATUS:
            st = unpack_control_status(buf)
            self._emit("on_control_status", st)
            return header, st
        if header.kind == KIND_LOG:
            return header, unpack_log(buf)
        return header, None

    def poll(self, now: float) -> MatchEnded | None:
        ev = self.boundary.poll(now)
        self._dispatch(ev)
        return ev

    def close(self, now: float) -> MatchEnded | None:
        ev = self.boundary.close(now)
        self._dispatch(ev)
        return ev


class VersionGate:
    """Tracks heartbeat game versions around one match.

    The last heartbeat before the match counts (heartbeats come every 500 ms,
    so one usually lands before frame 0). A match is accepted only if every
    version seen for it is the expected one.
    """

    def __init__(self, expect_version: str | None) -> None:
        self.expect = expect_version
        self.current: str | None = None
        self.seen: set[str] = set()

    def heartbeat(self, version: str) -> None:
        self.current = version
        self.seen.add(version)

    def start_match(self) -> None:
        self.seen = {self.current} if self.current is not None else set()

    def verdict(self) -> tuple[bool, str, str | None]:
        """(ok, reason, version). `version` is what goes in the log header."""
        if not self.seen:
            return False, "no heartbeat seen, can't check the game version", None
        if len(self.seen) > 1:
            return False, f"game version changed mid-match: {sorted(self.seen)}", None
        (version,) = self.seen
        if self.expect and version != self.expect:
            return False, f"game version {version}, expected {self.expect}", version
        return True, "ok", version
