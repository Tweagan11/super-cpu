"""Discretized action space (SSB-15).

Main stick: center, 8 directions at full tilt, 8 at half tilt = 17 classes.
C-stick: center + 4 cardinals = 5 classes.
Buttons: a short list of the combos that matter, filled in once the v1
character is picked (SSB-8).
"""
from __future__ import annotations

import math

FULL, HALF = 127, 64
_DIRS = [(math.cos(a), math.sin(a)) for a in (i * math.pi / 4 for i in range(8))]

MAIN_STICK: list[tuple[int, int]] = [(0, 0)]
MAIN_STICK += [(round(FULL * x), round(FULL * y)) for x, y in _DIRS]
MAIN_STICK += [(round(HALF * x), round(HALF * y)) for x, y in _DIRS]

C_STICK: list[tuple[int, int]] = [(0, 0), (FULL, 0), (0, FULL), (-FULL, 0), (0, -FULL)]

# TODO(SSB-15): fill from logged data for the v1 character. Bitmask values
# must match the plugin's button layout.
BUTTON_COMBOS: list[int] = [0]


def nearest(table: list[tuple[int, int]], x: int, y: int) -> int:
    return min(range(len(table)), key=lambda i: (table[i][0] - x) ** 2 + (table[i][1] - y) ** 2)
