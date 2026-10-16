"""Hands-off rematches (SSB-25): results screen -> CSS -> next match.

Driven by the scene packets (wire.SceneInfo) and the match boundary events
from matches.MatchWatcher. After a match ends:

- on the results screen, press A until the scene changes
- on the CSS, press + (start the match; the picks are kept from last time)

Every press is one menu press packet (kind 8) via the `press` callback. The
wait between presses on the same scene visit grows (BACKOFF), and after
MAX_PRESSES with no scene change it gives up on that visit and says so, so a
wrong scene name or a press that never registers can't spam the game. It
never presses on a scene it doesn't recognize, or when scene packets stopped
arriving (STALE_AFTER).

Scene names are guesses until hardware experiment E1; this logs every scene
it sees, with the class it gave it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import menu_buttons as MB
from .scenes import CSS, DEFAULT_NAMES, OTHER, RESULTS, SceneNames

SETTLE_S = 1.0       # after entering a scene, wait this long before the first press
BACKOFF = 1.6        # each retry waits this much longer than the last
MAX_WAIT_S = 6.0     # ...but never more than this
MAX_PRESSES = 12     # per scene visit, then give up until the scene changes
STALE_AFTER_S = 3.5  # scene packets come every 1 s; older than this means we don't know
PRESS_HOLD_FRAMES = 1  # one pulse. More frames means more pulses (1 on, 2 off).

# which button each scene wants
PRESS_FOR = {RESULTS: MB.A, CSS: MB.PLUS}


@dataclass
class _Visit:
    changes: int
    name: str
    kind: str
    since: float
    presses: int = 0
    next_at: float = 0.0
    gave_up: bool = False


class Rematch:
    """MatchWatcher observer. Call `tick(now)` regularly."""

    def __init__(self, press: Callable[[int, int], None], log: Callable[[str], None] = print,
                 names: SceneNames = DEFAULT_NAMES, settle: float = SETTLE_S,
                 max_presses: int = MAX_PRESSES) -> None:
        self.press = press
        self.log = log
        self.names = names
        self.settle = settle
        self.max_presses = max_presses
        self.armed = False  # a match has ended since the last one started
        self.in_match = False
        self.visit: _Visit | None = None
        self.last_scene_at: float | None = None
        self.presses_total = 0
        self.matches_seen = 0
        self._unknown: set[str] = set()

    # --- MatchWatcher observer hooks
    def on_match_start(self, ev) -> None:
        self.in_match = True
        self.armed = False
        self.matches_seen += 1

    def on_match_end(self, ev) -> None:
        self.in_match = False
        self.armed = True
        self.log("[rematch] match ended, will press through results and the CSS")

    def on_scene(self, scene, now: float) -> None:
        self.last_scene_at = now
        v = self.visit
        if v is not None and v.changes == scene.changes and v.name == scene.name:
            return
        kind = self.names.classify(scene.name)
        self.visit = _Visit(scene.changes, scene.name, kind, now, next_at=now + self.settle)
        self.log(f"[rematch] scene #{scene.changes}: {scene.name!r} ({kind})")
        if kind == OTHER and scene.name not in self._unknown:
            self._unknown.add(scene.name)
            self.log(f"[rematch] {scene.name!r} isn't a known scene; not pressing there. "
                     f"If it is the results screen or the CSS, fix the names (scenes.py).")

    # --- timer
    def tick(self, now: float) -> bool:
        """Presses if it's time. Returns True if a press was sent."""
        v = self.visit
        if v is None or not self.armed or self.in_match or v.gave_up:
            return False
        button = PRESS_FOR.get(v.kind)
        if button is None:
            return False
        if self.last_scene_at is None or now - self.last_scene_at > STALE_AFTER_S:
            return False
        if now < v.next_at:
            return False
        if v.presses >= self.max_presses:
            v.gave_up = True
            self.log(f"[rematch] giving up on {v.name!r}: {v.presses} presses and the scene didn't change. "
                     f"Press by hand to continue.")
            return False
        v.presses += 1
        self.presses_total += 1
        wait = min(MAX_WAIT_S, self.settle * BACKOFF ** v.presses)
        v.next_at = now + wait
        self.log(f"[rematch] {'A' if button == MB.A else '+'} on {v.name!r}, press {v.presses}/{self.max_presses}, "
                 f"next in {wait:.1f}s")
        self.press(button, PRESS_HOLD_FRAMES)
        return True
