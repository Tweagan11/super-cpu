"""Sanity check DAgger data and compare rounds (SSB-18).

    python -m training.dagger_report data/cpu data/dagger1 data/dagger2 data/dagger3

Two parts:

1. Results by model: every bot-played match grouped by its controller label
   (`bot:<--model>`), with wins, damage dealt and stocks taken. This is how
   to tell whether a round actually played better than the last.

2. Label check: compares three input streams on the bot's port.
   - cpu: the level 9 CPU playing for itself (CPU vs CPU logs)
   - expert: the CPU AI's decision on frames the bot drove (the DAgger labels)
   - played: what the bot actually pressed on those frames
   If the expert labels look nothing like the CPU playing for itself (far
   more button presses, one button held for ages, never steering back to
   the stage when offstage), the AI's answers while it isn't in control are
   bad labels, and training on them makes the bot worse.
"""
from __future__ import annotations

import argparse
from collections import defaultdict

import numpy as np

from bridge.logger import EXPERT_FIELDS, INPUT_FIELDS, load_match
from bridge.measure import OFFSTAGE_STATUSES
from bridge.stages import LAYOUTS, LAYOUT_FD

from . import actions as A
from .features import find_matches

BUTTON_NAMES = ["none", "attack", "special", "jump", "short hop", "shield", "grab", "shield+jump", "attack+jump"]


class Stream:
    def __init__(self) -> None:
        self.main: list[np.ndarray] = []
        self.btn: list[np.ndarray] = []
        self.changed = 0
        self.held = 0
        self.pressed = 0
        self.n = 0
        self.off_n = 0
        self.off_toward = 0
        self.off_up = 0
        self.off_special = 0
        self.off_jump = 0
        self.off_up_special = 0

    def add(self, main, cs, btn, offstage, toward) -> None:
        if len(main) == 0:
            return
        self.n += len(main)
        self.btn.append(btn)
        tup = main * 100 + cs * 10 + btn
        self.changed += int((tup[1:] != tup[:-1]).sum())
        pressed = btn != 0
        self.pressed += int(pressed.sum())
        self.held += int((pressed[1:] & (btn[1:] == btn[:-1])).sum())
        sx = A._MAIN_ARR[main, 0]
        sy = A._MAIN_ARR[main, 1]
        o = offstage
        self.off_n += int(o.sum())
        self.off_toward += int(((np.sign(sx) == toward) & (sx != 0) & o).sum())
        self.off_up += int(((sy > 0) & o).sum())
        self.off_special += int(((btn == 2) & o).sum())
        self.off_jump += int((np.isin(btn, (3, 4, 7, 8)) & o).sum())
        self.off_up_special += int(((btn == 2) & (sy > 0) & o).sum())

    def report(self, name: str) -> list[str]:
        if self.n == 0:
            return [f"  {name:8s} no rows"]
        b = np.bincount(np.concatenate(self.btn), minlength=len(BUTTON_NAMES)) / self.n
        pct = lambda x, d: f"{100 * x / d:5.1f}%" if d else "    -"
        lines = [
            f"  {name:8s} {self.n:>9,} rows  input changes {pct(self.changed, self.n)}/frame"
            f"  button held from last frame {pct(self.held, self.pressed)} of presses",
            "           buttons: " + "  ".join(f"{n} {100 * v:.1f}%" for n, v in zip(BUTTON_NAMES, b) if v >= 0.001),
            f"           offstage {self.off_n:,} rows: stick toward stage {pct(self.off_toward, self.off_n)}"
            f"  stick up {pct(self.off_up, self.off_n)}  jump {pct(self.off_jump, self.off_n)}"
            f"  special {pct(self.off_special, self.off_n)}  up-special {pct(self.off_up_special, self.off_n)}",
        ]
        return lines


def peak_damage(p) -> tuple[float, int]:
    """Damage dealt counted a second way: the highest percent the opponent
    reached on each stock, summed. Unlike the logger's running total of
    frame-to-frame increases, a damage reading that flickers down and back up
    can't inflate this. Also returns how many frames damage went down without
    a stock being lost (should be ~0; lots of them means flicker)."""
    d = p["damage"].astype(np.float64)
    st = p["stocks"].astype(np.int64)
    lost = np.zeros(len(d), bool)
    lost[1:] = (st[:-1] > 0) & (st[1:] < st[:-1])
    seg = np.cumsum(lost)
    total = sum(float(d[seg == k].max()) for k in np.unique(seg)) if len(d) else 0.0
    drops = int(((d[1:] < d[:-1] - 0.01) & ~lost[1:]).sum())
    return total, drops


def classes(p, fields, mask):
    m, c, b = A.encode_arrays(*(p[k][mask] for k in fields))
    return np.asarray(m), np.asarray(c), np.asarray(b)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", nargs="+", help="match dirs or .npz files")
    ap.add_argument("--bot-port", type=int, default=2, choices=(1, 2))
    ap.add_argument("--bad-damage", action="store_true",
                    help="only list matches whose damage readings look broken (flicker or impossible jumps), with fighters")
    args = ap.parse_args(argv)
    paths = find_matches(args.data)
    if not paths:
        raise SystemExit("no .npz files found")

    bot, opp = args.bot_port, 3 - args.bot_port
    streams = {k: Stream() for k in ("cpu", "expert", "played")}
    agree = np.zeros(4, np.int64)  # main, cstick, buttons, all
    agree_n = 0
    results: dict[str, list[tuple[bool, float, int, float, float, int]]] = defaultdict(list)
    skipped = 0

    for path in paths:
        try:
            m = load_match(path)
        except Exception as e:  # noqa: BLE001 - report and move on
            print(f"skip {path.name}: {e}")
            skipped += 1
            continue
        h = m.header
        if args.bad_damage:
            ports = h.get("ports", [])
            for slot in (1, 2):
                peak, drops = peak_damage(m.port(slot))
                logged = float(ports[slot - 1].get("damage_taken", 0.0)) if len(ports) >= slot else 0.0
                if drops > 10 or logged > peak + 50:
                    names = [q.get("fighter_name", "?") for q in ports]
                    print(f"{path.name}: P{slot} {names[slot - 1] if len(names) >= slot else '?'} logged {logged:.0f}% "
                          f"vs peak-based {peak:.0f}%, {drops} drops without a KO  (fighters {names})")
            continue
        ports = h.get("ports", [])
        ctrl = ports[bot - 1].get("controller", "?") if len(ports) >= bot else "?"
        p = m.port(bot)
        layout = LAYOUTS.get(h.get("stage_layout") or LAYOUT_FD, LAYOUTS[LAYOUT_FD])
        alive = ~np.isin(p["status_kind"], list(OFFSTAGE_STATUSES))
        rel = p["pos_x"] - layout.center_x
        offstage = (np.abs(rel) > layout.ledge_x) & alive
        toward = -np.sign(rel)
        exp = p["expert_valid"] == 1

        if exp.any():
            if len(ports) >= opp and h.get("complete"):
                o = ports[opp - 1]
                peak, drops = peak_damage(m.port(opp))
                results[ctrl].append((h.get("winner_port") == bot, float(o.get("damage_taken", 0.0)), int(o.get("stocks_lost", 0)),
                                      float(h.get("duration_s") or 0.0), peak, drops))
            streams["expert"].add(*classes(p, EXPERT_FIELDS, exp), offstage[exp], toward[exp])
            pl = exp & (p["input_valid"] == 1)
            streams["played"].add(*classes(p, INPUT_FIELDS, pl), offstage[pl], toward[pl])
            both = exp & (p["input_valid"] == 1)
            if both.any():
                e = classes(p, EXPERT_FIELDS, both)
                q = classes(p, INPUT_FIELDS, both)
                eq = [e[i] == q[i] for i in range(3)]
                agree[:3] += [int(x.sum()) for x in eq]
                agree[3] += int((eq[0] & eq[1] & eq[2]).sum())
                agree_n += int(both.sum())
        elif not ctrl.startswith("bot"):
            ok = p["input_valid"] == 1
            streams["cpu"].add(*classes(p, INPUT_FIELDS, ok), offstage[ok], toward[ok])

    print(f"{len(paths) - skipped} matches read" + (f", {skipped} skipped" if skipped else ""))
    print("\nResults by model (bot-played matches):")
    if not results:
        print("  none (no matches with expert rows)")
    for ctrl, rows in sorted(results.items()):
        n = len(rows)
        wins = sum(r[0] for r in rows)
        dmg = np.mean([r[1] for r in rows])
        stocks = np.mean([r[2] for r in rows])
        secs = np.mean([r[3] for r in rows])
        mins = max(1e-9, sum(r[3] for r in rows) / 60)
        per_min = sum(r[1] for r in rows) / mins
        peak = np.mean([r[4] for r in rows])
        peak_min = sum(r[4] for r in rows) / mins
        drops = np.mean([r[5] for r in rows])
        print(f"  {ctrl:28s} {n:3d} matches  wins {wins}/{n}  stocks taken {stocks:.2f}  match {secs:4.0f}s")
        print(f"  {'':28s} damage dealt: logged {dmg:5.1f}% ({per_min:5.1f}/min)  peak-based {peak:5.1f}% ({peak_min:5.1f}/min)"
              f"  damage drops without a KO {drops:.1f}/match")

    print("\nLabel check (bot port, split as cpu playing itself / expert labels / bot's own inputs):")
    for k in ("cpu", "expert", "played"):
        for line in streams[k].report(k):
            print(line)
    if agree_n:
        a = agree / agree_n
        print(f"\n  expert vs played on the same frames: main {a[0]:.3f} cstick {a[1]:.3f} buttons {a[2]:.3f} all {a[3]:.3f}")
    print("\nWhat to look for: expert should look roughly like cpu. Much higher input changes,")
    print("buttons held far longer, or much less 'toward stage' / up-special offstage than cpu")
    print("means the AI's answers while the bot drives are bad labels.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
