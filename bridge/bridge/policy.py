"""Policy interface. Every bot, scripted or learned, is one call:
state history in, inputs out.
"""
from __future__ import annotations

from collections import deque
from typing import Protocol, Sequence

from .protocol import Frame, Inputs


class Policy(Protocol):
    def act(self, history: Sequence[Frame]) -> Inputs: ...


class IdlePolicy:
    """Does nothing. Useful for checking the loop end to end (SSB-5)."""

    def act(self, history: Sequence[Frame]) -> Inputs:
        return Inputs()


class WalkTowardPolicy:
    """Smoke test for SSB-6: walk toward the opponent."""

    def act(self, history: Sequence[Frame]) -> Inputs:
        f = history[-1]
        dx = f.opponent.pos_x - f.bot.pos_x
        stick = 0 if abs(dx) < 10 else (100 if dx > 0 else -100)
        return Inputs(stick_x=stick)


class History:
    """Fixed-length window of recent frames fed to the policy."""

    def __init__(self, length: int = 16) -> None:
        self._frames: deque[Frame] = deque(maxlen=length)

    def push(self, frame: Frame) -> Sequence[Frame]:
        self._frames.append(frame)
        return tuple(self._frames)
