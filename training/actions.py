"""Discretized action space (SSB-15).

Three heads, one class each per frame:

- Main stick: center, 8 directions at full tilt, 8 at half tilt = 17 classes.
  Half tilt is there for tilts and walking; full tilt for dash, smash
  inputs, DI and recovery angles.
- C-stick: center + 4 cardinals = 5 classes. With default controls the
  c-stick is smash attacks (and aerials in the air), which only care about
  the cardinal direction.
- Buttons: 9 combos, below.

Button combos for R.O.B., and why each is there:

| idx | combo | what it's for |
| --- | --- | --- |
| 0 | none | most frames |
| 1 | attack | jab, tilts (with half stick), smashes (with full stick), aerials |
| 2 | special | all of R.O.B.'s specials: laser, gyro, arm rotor, Robo Burner (held) |
| 3 | jump | full hop, double jump, jump out of shield |
| 4 | jump_mini | short hop (the short hop button bit) |
| 5 | guard | shield; with the stick it's spot dodge, roll, air dodge |
| 6 | catch | grab. guard+attack (shield grab) maps here too |
| 7 | guard+jump | jump out of shield while still holding shield, which humans do all the time |
| 8 | attack+jump | jump+attack on the same frame, Ultimate's short hop aerial shortcut |

Left out on purpose: taunts and stock share (useless), the `*_RAW` and
`GUARD_HOLD` duplicates (folded into their main bit by `canonical_buttons`),
`SMASH`, `FLICK_JUMP` and `CSTICK_ON` (side effects of stick input, not
buttons a policy chooses). Anything else that doesn't match a combo falls
back to the single most important button it contains (see `_PRIORITY`).

Run `python -m training.actions data/matches/*.npz --port 1` on real logs
to see how much survives encode -> decode. If some combo shows up a lot in the
"collapsed" list, add it here. Changing BUTTON_COMBOS changes the model's
output size, so old checkpoints won't load (bc_policy checks this).
"""
from __future__ import annotations

import argparse
import math
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from bridge import buttons as B
from bridge.protocol import Inputs

FULL, HALF = 127, 64
_DIRS = [(math.cos(a), math.sin(a)) for a in (i * math.pi / 4 for i in range(8))]

MAIN_STICK: list[tuple[int, int]] = [(0, 0)]
MAIN_STICK += [(round(FULL * x), round(FULL * y)) for x, y in _DIRS]
MAIN_STICK += [(round(HALF * x), round(HALF * y)) for x, y in _DIRS]

C_STICK: list[tuple[int, int]] = [(0, 0), (FULL, 0), (0, FULL), (-FULL, 0), (0, -FULL)]

BUTTON_COMBOS: list[int] = [
    B.NONE,
    B.ATTACK,
    B.SPECIAL,
    B.JUMP,
    B.JUMP_MINI,
    B.GUARD,
    B.CATCH,
    B.GUARD | B.JUMP,
    B.ATTACK | B.JUMP,
]
BUTTON_NAMES: list[str] = [
    "none", "attack", "special", "jump", "jump_mini", "guard", "catch", "guard+jump", "attack+jump",
]

N_MAIN, N_CSTICK, N_BUTTONS = len(MAIN_STICK), len(C_STICK), len(BUTTON_COMBOS)

# Bits the action space can express. Everything else is dropped.
KEPT_BITS = B.ATTACK | B.SPECIAL | B.JUMP | B.JUMP_MINI | B.GUARD | B.CATCH
# Duplicate bits folded into their main one.
_FOLD = {
    B.ATTACK_RAW: B.ATTACK,
    B.SPECIAL_RAW: B.SPECIAL,
    B.SPECIAL_RAW2: B.SPECIAL,
    B.GUARD_HOLD: B.GUARD,
}
# Combos that mean the same thing as one in the table.
_ALIASES = {B.GUARD | B.ATTACK: B.CATCH}
# Fallback for combos not in the table: keep the first of these that's pressed.
_PRIORITY = [B.SPECIAL, B.ATTACK, B.CATCH, B.JUMP, B.JUMP_MINI, B.GUARD]

_MAIN_ARR = np.array(MAIN_STICK, dtype=np.int32)
_C_ARR = np.array(C_STICK, dtype=np.int32)
_COMBO_INDEX = {c: i for i, c in enumerate(BUTTON_COMBOS)}


def nearest(table: list[tuple[int, int]], x: int, y: int) -> int:
    return min(range(len(table)), key=lambda i: (table[i][0] - x) ** 2 + (table[i][1] - y) ** 2)


def canonical_buttons(buttons: int) -> int:
    """Fold duplicate bits into their main bit and drop the ones we don't model."""
    b = int(buttons)
    for dup, main in _FOLD.items():
        if b & dup:
            b |= main
    return b & KEPT_BITS


def button_class(buttons: int) -> int:
    b = canonical_buttons(buttons)
    b = _ALIASES.get(b, b)
    if b in _COMBO_INDEX:
        return _COMBO_INDEX[b]
    for bit in _PRIORITY:
        if b & bit:
            return _COMBO_INDEX[bit]
    return 0


# 2^18 covers every bit in buttons.py; anything above is masked off first.
_BUTTON_LUT_BITS = 18
_BUTTON_LUT = np.array([button_class(b) for b in range(1 << _BUTTON_LUT_BITS)], dtype=np.int64)


def _nearest_arr(table: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.int32)[..., None]
    y = np.asarray(y, dtype=np.int32)[..., None]
    d = (table[:, 0] - x) ** 2 + (table[:, 1] - y) ** 2
    return np.argmin(d, axis=-1).astype(np.int64)  # ties go to the lower index, same as nearest()


@dataclass(frozen=True)
class ActionClasses:
    main: int
    cstick: int
    buttons: int


def encode(inp: Inputs) -> ActionClasses:
    """Logged/raw inputs -> class index per head."""
    return ActionClasses(
        nearest(MAIN_STICK, inp.stick_x, inp.stick_y),
        nearest(C_STICK, inp.cstick_x, inp.cstick_y),
        button_class(inp.buttons),
    )


def decode(main: int, cstick: int, buttons: int) -> Inputs:
    """Class indices -> the Inputs the bridge sends."""
    sx, sy = MAIN_STICK[main]
    cx, cy = C_STICK[cstick]
    return Inputs(stick_x=sx, stick_y=sy, cstick_x=cx, cstick_y=cy, buttons=BUTTON_COMBOS[buttons])


def encode_arrays(stick_x, stick_y, cstick_x, cstick_y, buttons) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorized `encode` over arrays of any (matching) shape."""
    main = _nearest_arr(_MAIN_ARR, stick_x, stick_y)
    c = _nearest_arr(_C_ARR, cstick_x, cstick_y)
    b = np.asarray(buttons).astype(np.int64) & ((1 << _BUTTON_LUT_BITS) - 1)
    return main, c, _BUTTON_LUT[b]


def decode_arrays(main, cstick, buttons) -> tuple[np.ndarray, ...]:
    """Vectorized `decode`: (stick_x, stick_y, cstick_x, cstick_y, buttons) arrays."""
    m, c = _MAIN_ARR[np.asarray(main)], _C_ARR[np.asarray(cstick)]
    return m[..., 0], m[..., 1], c[..., 0], c[..., 1], np.array(BUTTON_COMBOS, dtype=np.int64)[np.asarray(buttons)]


# How lossy is the action space on a real dataset?

@dataclass
class RoundTripReport:
    rows: int = 0               # rows with input_valid = 1
    exact: int = 0              # all five fields survive encode -> decode bit for bit
    main_exact: int = 0
    cstick_exact: int = 0
    buttons_exact: int = 0
    buttons_canonical: int = 0  # buttons match after canonical_buttons() (dropped bits ignored)
    dropped_bits: Counter = None   # bit -> rows where that bit was set but isn't kept
    collapsed: Counter = None      # canonical combo not in the table -> rows

    def __post_init__(self) -> None:
        self.dropped_bits = self.dropped_bits or Counter()
        self.collapsed = self.collapsed or Counter()

    def add(self, sx, sy, cx, cy, btn) -> None:
        sx, sy, cx, cy = (np.asarray(a, dtype=np.int64) for a in (sx, sy, cx, cy))
        btn = np.asarray(btn, dtype=np.int64)
        m, c, b = encode_arrays(sx, sy, cx, cy, btn)
        dsx, dsy, dcx, dcy, db = decode_arrays(m, c, b)
        me = (dsx == sx) & (dsy == sy)
        ce = (dcx == cx) & (dcy == cy)
        be = db == btn
        canon = np.array([canonical_buttons(x) for x in btn], dtype=np.int64)
        self.rows += len(btn)
        self.exact += int((me & ce & be).sum())
        self.main_exact += int(me.sum())
        self.cstick_exact += int(ce.sum())
        self.buttons_exact += int(be.sum())
        self.buttons_canonical += int((db == canon).sum())
        for bit in range(_BUTTON_LUT_BITS):
            mask = 1 << bit
            if not mask & KEPT_BITS:
                n = int(((btn & mask) != 0).sum())
                if n:
                    self.dropped_bits[mask] += n
        for x in canon[db != canon]:
            self.collapsed[int(x)] += 1

    def text(self) -> str:
        def pct(n: int) -> str:
            return f"{n}/{self.rows} ({100 * n / max(1, self.rows):.1f}%)"
        names = {getattr(B, k): k.lower() for k in dir(B) if k.isupper() and isinstance(getattr(B, k), int)}
        lines = [
            f"rows with inputs:      {self.rows}",
            f"exact round trip:      {pct(self.exact)}",
            f"  main stick exact:    {pct(self.main_exact)}",
            f"  c-stick exact:       {pct(self.cstick_exact)}",
            f"  buttons exact:       {pct(self.buttons_exact)}",
            f"  buttons, kept bits:  {pct(self.buttons_canonical)}",
        ]
        if self.dropped_bits:
            lines.append("dropped bits (rows): " + ", ".join(
                f"{names.get(k, hex(k))}={v}" for k, v in self.dropped_bits.most_common()))
        if self.collapsed:
            lines.append("combos not in the table (rows): " + ", ".join(
                f"{'+'.join(n for b, n in names.items() if b and k & b and b & KEPT_BITS) or hex(k)}={v}"
                for k, v in self.collapsed.most_common(10)))
        return "\n".join(lines)


def roundtrip_report(matches: Iterable, port: int) -> RoundTripReport:
    """How much of a dataset's inputs survive encode -> decode. `matches` are
    `bridge.logger.LoadedMatch`es; only rows with input_valid = 1 count."""
    rep = RoundTripReport()
    for m in matches:
        p = m.port(port)
        ok = p["input_valid"] == 1
        rep.add(p["stick_x"][ok], p["stick_y"][ok], p["cstick_x"][ok], p["cstick_y"][ok], p["buttons"][ok])
    return rep


def main() -> None:
    from bridge.logger import load_match

    ap = argparse.ArgumentParser(description="encode -> decode survival report for logged inputs")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--port", type=int, default=1)
    ap.add_argument("--expect-version", default="13.0.4")
    args = ap.parse_args()
    print(roundtrip_report((load_match(f, expect_version=args.expect_version) for f in args.files), args.port).text())


if __name__ == "__main__":
    main()
