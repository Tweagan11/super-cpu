"""Measure stage geometry from the live state stream.

    python -m bridge.measure

Start a match on the stage you want to measure, then walk a fighter to both
edges of the main stage and onto every platform (stand still for a moment on
each). When the match ends, or when you press Ctrl+C, this prints the ledges,
ground height and platforms it saw, plus a `StageLayout(...)` you can paste
into bridge/stages.py.

It only uses frames where a fighter is standing on something (not airborne,
not hanging on a ledge). Positions are the fighter's origin, so "ledge" means
how far out a fighter can stand, which is what the scripted bot needs.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field

from .matches import MatchEnded, MatchStarted
from .protocol import Frame
from .stages import stage_layout, stage_name
from .statuses import STATUS_NAMES

# Hanging on the ledge counts as "not airborne" for some statuses; skip them.
CLIFF_STATUSES = frozenset(k for k, v in STATUS_NAMES.items() if v.startswith("CLIFF"))
# Surfaces closer than this (game units) count as the same height.
HEIGHT_BUCKET = 2.0
# A surface needs this many standing frames to count (filters one-frame noise).
MIN_FRAMES = 10


@dataclass
class Surface:
    y_sum: float = 0.0
    frames: int = 0
    left: float = float("inf")
    right: float = float("-inf")

    def add(self, x: float, y: float) -> None:
        self.y_sum += y
        self.frames += 1
        self.left = min(self.left, x)
        self.right = max(self.right, x)

    @property
    def y(self) -> float:
        return self.y_sum / self.frames


@dataclass
class StageMeasurer:
    """MatchWatcher observer that collects standing positions per height."""

    echo: object = print
    surfaces: dict[int, Surface] = field(default_factory=lambda: defaultdict(Surface))
    stage_id: int | None = None

    def on_heartbeat(self, hb) -> None:
        pass

    def on_match_start(self, ev: MatchStarted) -> None:
        self.surfaces = defaultdict(Surface)
        self.stage_id = ev.first.stage_id
        self.echo(
            f"[measure] measuring {stage_name(self.stage_id)}: walk to both edges "
            "and stand on every platform"
        )

    def on_frame(self, frame: Frame, seq: int, now: float) -> None:
        for f in (frame.bot, frame.opponent):
            if f.airborne or f.status_kind in CLIFF_STATUSES or f.stocks == 0:
                continue
            self.surfaces[round(f.pos_y / HEIGHT_BUCKET)].add(f.pos_x, f.pos_y)

    def on_match_end(self, ev: MatchEnded) -> None:
        self.report()

    def report(self) -> str | None:
        found = sorted(
            (s for s in self.surfaces.values() if s.frames >= MIN_FRAMES), key=lambda s: s.y
        )
        if not found:
            self.echo("[measure] not enough standing frames to measure anything yet")
            return None
        # The main stage is the widest surface; platforms are the ones above it.
        ground = max(found, key=lambda s: s.right - s.left)
        platforms = [s for s in found if s is not ground and s.y > ground.y + HEIGHT_BUCKET]
        center = (ground.left + ground.right) / 2
        half = (ground.right - ground.left) / 2
        layout = stage_layout(self.stage_id) if self.stage_id is not None else None

        lines = [
            f"[measure] {stage_name(self.stage_id) if self.stage_id is not None else 'unknown stage'} "
            f"(layout {layout or 'other'})",
            f"  main stage: x {ground.left:.1f} to {ground.right:.1f}, ground y {ground.y:.1f}, "
            f"{ground.frames} frames",
        ]
        for p in platforms:
            lines.append(
                f"  platform:   x {p.left:.1f} to {p.right:.1f}, y {p.y:.1f}, {p.frames} frames"
            )
        plat_code = "".join(
            f"\n        Platform({p.left:.1f}, {p.right:.1f}, {p.y - ground.y:.1f}),"
            for p in platforms
        )
        lines.append("  for bridge/stages.py:")
        lines.append(
            f"    StageLayout(\n        name={layout or 'other'!r},\n        center_x={center:.1f},\n"
            f"        ledge_x={half:.1f},\n        ground_y={ground.y:.1f},\n"
            f"        platforms=({plat_code}\n        ),\n    )"
        )
        lines.append(
            "  Ledges are only as good as how close you got to each edge. "
            "Walk right up to the teeter animation on both sides."
        )
        text = "\n".join(lines)
        self.echo(text)
        return text


def main() -> None:
    from .udp_listen import listen, make_watcher

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4243)
    ap.add_argument("--expect-version", default="13.0.4")
    args = ap.parse_args()

    measurer = StageMeasurer()
    try:
        listen(args.port, args.expect_version, watcher=make_watcher([measurer]))
    except KeyboardInterrupt:
        print()
        measurer.report()


if __name__ == "__main__":
    main()
