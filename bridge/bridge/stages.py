"""Stage layouts for the v1 matchup (SSB-8): Battlefield and Final Destination,
Omega forms included (Omega uses the FD layout).

stage_id isn't streamed yet (the plugin sends 0), so the layout comes from a
flag for now. `resolve_layout` is the one place that picks it: once real
stage ids arrive, fill in `STAGE_ID_LAYOUTS` and everything that calls
`resolve_layout(frame.stage_id, fallback)` switches automatically.

The numbers below are rough, conservative guesses in game units, not measured
values. Measure them on hardware (stand at each ledge and on each platform,
read pos from udp_listen) before trusting the scripted bot near edges.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Platform:
    left: float
    right: float
    y: float


@dataclass(frozen=True)
class StageLayout:
    name: str
    center_x: float = 0.0
    ledge_x: float = 80.0      # |x| of the main stage ledges, measured from center_x
    ground_y: float = 0.0      # main stage surface
    platforms: tuple[Platform, ...] = ()

    def offstage(self, x: float, margin: float = 0.0) -> bool:
        return abs(x - self.center_x) > self.ledge_x - margin

    def below_stage(self, y: float) -> bool:
        return y < self.ground_y


# TODO(measure): ledge and platform numbers are estimates.
BATTLEFIELD = StageLayout(
    name="battlefield",
    ledge_x=70.0,
    platforms=(
        Platform(-55.0, -20.0, 27.0),
        Platform(20.0, 55.0, 27.0),
        Platform(-18.0, 18.0, 54.0),
    ),
)
FINAL_DESTINATION = StageLayout(name="fd", ledge_x=85.0)

LAYOUTS: dict[str, StageLayout] = {
    "battlefield": BATTLEFIELD,
    "fd": FINAL_DESTINATION,
}
# Accepted spellings for --stage. Omega forms share the FD layout.
ALIASES: dict[str, str] = {
    "bf": "battlefield",
    "battlefield": "battlefield",
    "fd": "fd",
    "final_destination": "fd",
    "omega": "fd",
}

# Real stage_id -> layout name. Empty until the plugin streams stage_id.
# 0 means "not sent", never a real stage, so it's never looked up here.
STAGE_ID_LAYOUTS: dict[int, str] = {}


def layout_by_name(name: str) -> StageLayout:
    key = ALIASES.get(name.lower())
    if key is None:
        raise ValueError(f"unknown stage {name!r}, use one of {sorted(ALIASES)}")
    return LAYOUTS[key]


def resolve_layout(stage_id: int, fallback: StageLayout) -> StageLayout:
    """Layout for a streamed stage_id, or the flag/config fallback."""
    name = STAGE_ID_LAYOUTS.get(stage_id) if stage_id else None
    return LAYOUTS[name] if name else fallback


def layout_name(stage_id: int, fallback: str) -> str:
    """Layout name for logs and eval rows."""
    return resolve_layout(stage_id, layout_by_name(fallback)).name
