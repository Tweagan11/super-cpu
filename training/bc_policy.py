"""A `bridge.policy.Policy` that plays a behavior cloning checkpoint (SSB-16).

    SCPU_CHECKPOINT=checkpoints/<run>/best.pt python -m bridge.run --policy bc

Each frame: live features from the history (training/features.py, the same
core the trainer used), one forward pass, argmax per head, decode to Inputs
(training/actions.py).

Config (env vars, since `--policy bc` builds it with no args; keyword args win):

- SCPU_CHECKPOINT: path to a .pt from train_bc (required)
- SCPU_DELAY: input delay in frames to plan for. Default: the checkpoint's
  max_delay (the worst case it was trained on). Set it to what SSB-7 measured.
- SCPU_BOT_PORT: 1 or 2, overrides the port the checkpoint was trained on
- SCPU_DEVICE: cpu (default) or cuda. CPU is plenty for the MLP.
"""
from __future__ import annotations

import os
from collections import deque
from dataclasses import replace
from typing import Sequence

import numpy as np
import torch

from bridge.protocol import Frame, Inputs

from . import actions as A
from .features import live_features
from .train_bc import HEADS, load_checkpoint


class BCPolicy:
    def __init__(
        self,
        checkpoint: str | os.PathLike | None = None,
        delay: int | None = None,
        bot_port: int | None = None,
        device: str | None = None,
    ) -> None:
        checkpoint = checkpoint or os.environ.get("SCPU_CHECKPOINT")
        if not checkpoint:
            raise ValueError("BCPolicy needs a checkpoint: set SCPU_CHECKPOINT=path/to/best.pt")
        self.device = device or os.environ.get("SCPU_DEVICE") or "cpu"
        self.model, self.ckpt = load_checkpoint(checkpoint, self.device)
        cfg = self.model.fcfg
        port = bot_port if bot_port is not None else os.environ.get("SCPU_BOT_PORT")
        if port:
            cfg = replace(cfg, bot_port=int(port))
        self.cfg = cfg
        env_delay = os.environ.get("SCPU_DELAY")
        self.delay = delay if delay is not None else int(env_delay) if env_delay else int(self.ckpt["max_delay"])
        if self.device == "cpu":
            torch.set_num_threads(1)  # tiny net: threads cost more than they save
        self.reset()

    def reset(self) -> None:
        """Forget own past inputs (call between matches)."""
        self.prev: deque[Inputs] = deque(maxlen=max(1, self.cfg.prev_inputs))
        self.last_frame: int | None = None
        self.last_classes: tuple[int, int, int] | None = None

    @torch.inference_mode()
    def predict(self, history: Sequence[Frame]) -> tuple[int, int, int]:
        cont, cat = live_features(history, list(self.prev), self.delay, self.cfg)
        out = self.model(torch.from_numpy(cont[None]).to(self.device), torch.from_numpy(cat[None]).to(self.device))
        return tuple(int(out[h][0].argmax()) for h in HEADS)

    def act(self, history: Sequence[Frame]) -> Inputs:
        if not history:
            return Inputs()
        f = history[-1].frame
        if self.last_frame is not None and f < self.last_frame:
            self.reset()  # frame counter went back: new match
        self.last_frame = f
        self.last_classes = self.predict(history)
        inp = A.decode(*self.last_classes)
        self.prev.append(inp)
        return inp
