"""Policy interface. Every bot, scripted or learned, is one call:
state history in, inputs out.
"""
from __future__ import annotations

from collections import deque
from typing import Callable, Protocol, Sequence

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


def _scripted() -> Policy:
    from .scripted import ScriptedRobPolicy  # lazy so policy.py stays import-light
    return ScriptedRobPolicy()


def _bc() -> Policy:
    """Behavior cloning checkpoint (training/bc_policy.py). Needs torch (the
    `train` extra) and SCPU_CHECKPOINT=path/to/best.pt. training/ lives next to
    bridge/ in the repo, not in this package, so put the repo root on the path."""
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    if (repo / "training" / "bc_policy.py").exists() and str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from training.bc_policy import BCPolicy
    return BCPolicy()


def _rl() -> Policy:
    """RL fine-tuning of a BC checkpoint (training/rl.py, SSB-21). Needs torch
    and SCPU_CHECKPOINT=path/to/best.pt (or SCPU_RL_RESUME=path/to/last.pt)."""
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    if (repo / "training" / "rl.py").exists() and str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from training.rl import RLPolicy
    return RLPolicy()


# Every selectable policy, by the name `--policy NAME` takes. Values are
# zero-arg factories, so a policy reads its own config (see scripted.py).
# Add new policies here and nowhere else.
POLICIES: dict[str, Callable[[], Policy]] = {
    "idle": IdlePolicy,
    "walk": WalkTowardPolicy,
    "scripted": _scripted,
    "bc": _bc,
    "rl": _rl,
}
