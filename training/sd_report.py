"""What happens when the bot falls off by itself (self-destructs).

    python -m training.sd_report data/rl-logs [--last 100] [--bot-port 2]

A self-destruct here is what training/rl.py counts: a bot stock lost with no
damage taken in the `--window` frames (default 120) before it. For each one,
looks back to the last frame the bot was on stage and reports how it left,
what it did out there and where it died, then sums it all up. That says
whether to fix recovery inputs (never uses up-B, wastes jumps), reward
(walks off on purpose) or something else (dies right after being hit and
the window is too short).
"""
from __future__ import annotations

import argparse
from collections import Counter

import numpy as np

from bridge.logger import load_match
from bridge.stages import LAYOUTS, LAYOUT_FD
from bridge.statuses import STATUS_NAMES

from .features import find_matches

SPECIAL = 0x2
DEAD_LIKE = {k for k, v in STATUS_NAMES.items() if v in ("DEAD", "REBIRTH", "ENTRY", "STANDBY")}
FALL_SPECIAL = {k for k, v in STATUS_NAMES.items() if v in ("FALL_SPECIAL", "LANDING_FALL_SPECIAL")}
CLIFF = {k for k, v in STATUS_NAMES.items() if v.startswith("CLIFF")}
KNOCKED = {k for k, v in STATUS_NAMES.items() if v.startswith("DAMAGE")}


def events(m, bot: int, window: int):
    p = m.port(bot)
    h = m.header
    lay = LAYOUTS.get(h.get("stage_layout") or LAYOUT_FD, LAYOUTS[LAYOUT_FD])
    st = p["stocks"].astype(int)
    dmg = p["damage"].astype(float)
    x = p["pos_x"] - lay.center_x
    y = p["pos_y"]
    status = p["status_kind"].astype(int)
    off = (np.abs(x) > lay.ledge_x) | (y < lay.ground_y - 10)
    alive = ~np.isin(status, list(DEAD_LIKE))
    for i in range(1, len(p)):
        if not (st[i - 1] > 0 and 0 <= st[i] < st[i - 1]):
            continue
        # the stock is lost on frame i; find the death frame (last alive frame before)
        d = i - 1
        while d > 0 and not alive[d]:
            d -= 1
        lo = max(0, d - window)
        hit_recently = (np.diff(dmg[lo:d + 1]) > 0.01).any()
        # start of the excursion: last frame on stage before death
        s = d
        while s > 0 and (off[s] or not alive[s]):
            s -= 1
        leave_status = STATUS_NAMES.get(int(status[min(s + 1, d)]), str(int(status[min(s + 1, d)])))
        exc = slice(s + 1, d + 1)
        ex = x[exc] if d > s else x[d:d + 1]
        ey = y[exc] if d > s else y[d:d + 1]
        btn = p["buttons"][exc].astype(np.int64)
        valid = p["input_valid"][exc] == 1
        up_b = int((((btn & SPECIAL) != 0) & (p["stick_y"][exc] > 40) & valid).sum())
        sts = status[exc]
        yield dict(
            self_destruct=not hit_recently,
            frames_out=d - s,
            left_by=("knocked" if int(status[min(s + 1, d)]) in KNOCKED else leave_status),
            jumps_at_leave=int(p["jumps_left"][min(s + 1, d)]),
            jumps_at_death=int(p["jumps_left"][d]),
            used_up_b=bool(up_b) or bool(np.isin(sts, list(FALL_SPECIAL)).any()),
            up_b_frames=up_b,
            grabbed_ledge=bool(np.isin(sts, list(CLIFF)).any()),
            died_below=bool(y[d] < lay.ground_y - 60 and abs(x[d]) < lay.ledge_x + 100),
            max_out=float(np.max(np.abs(ex)) - lay.ledge_x),
            min_y=float(np.min(ey)),
            stage=h.get("stage_layout"),
            match=h.get("match_id"),
        )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", nargs="+")
    ap.add_argument("--bot-port", type=int, default=2, choices=(1, 2))
    ap.add_argument("--window", type=int, default=120, help="frames without damage that make a stock loss a self-destruct")
    ap.add_argument("--last", type=int, default=None, help="only the newest N matches")
    ap.add_argument("--list", action="store_true", help="print every self-destruct")
    a = ap.parse_args(argv)
    paths = find_matches(a.data)
    paths = sorted(paths, key=lambda p: p.name)
    if a.last:
        paths = paths[-a.last:]
    evs, deaths, n = [], 0, 0
    for path in paths:
        try:
            m = load_match(path)
        except Exception as e:  # noqa: BLE001
            print(f"skip {path.name}: {e}")
            continue
        n += 1
        for e in events(m, a.bot_port, a.window):
            deaths += 1
            if e["self_destruct"]:
                evs.append(e)
                if a.list:
                    print(f"{e['match']}: out {e['frames_out']}f, left by {e['left_by']}, jumps {e['jumps_at_leave']}->{e['jumps_at_death']}, "
                          f"up-B {'yes' if e['used_up_b'] else 'NO'}, ledge {'yes' if e['grabbed_ledge'] else 'no'}, "
                          f"{'below' if e['died_below'] else 'side'}, {e['max_out']:.0f} past ledge, low y {e['min_y']:.0f}")
    print(f"{n} matches, {deaths} stocks lost, {len(evs)} self-destructs ({100 * len(evs) / max(1, deaths):.0f}%)")
    if not evs:
        return 0
    k = len(evs)
    pct = lambda c: f"{100 * c / k:.0f}%"
    print(f"  never used up-B:           {pct(sum(not e['used_up_b'] for e in evs))}")
    print(f"  still had a jump at death: {pct(sum(e['jumps_at_death'] > 0 for e in evs))}")
    print(f"  grabbed the ledge first:   {pct(sum(e['grabbed_ledge'] for e in evs))}")
    print(f"  died below the stage:      {pct(sum(e['died_below'] for e in evs))} (rest: off the side or top)")
    print(f"  frames offstage, median:   {int(np.median([e['frames_out'] for e in evs]))}")
    print(f"  distance past ledge, median: {np.median([e['max_out'] for e in evs]):.0f}")
    print("  how it left the stage:     " + ", ".join(f"{s} {pct(c)}" for s, c in Counter(e["left_by"] for e in evs).most_common(6)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
