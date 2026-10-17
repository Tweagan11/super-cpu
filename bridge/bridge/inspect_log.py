"""Print a summary of one or more match logs.

    python -m bridge.inspect_log data/matches/20261015-201500-a1b2c3.npz
    python -m bridge.inspect_log data/matches/*.npz --expect-version 13.0.4
"""
from __future__ import annotations

import argparse
import sys

import numpy as np

from .logger import LoadedMatch, LogRejected, load_match
from .statuses import status_name


def top_statuses(col: np.ndarray, top: int = 5) -> str:
    kinds, counts = np.unique(col["status_kind"], return_counts=True)
    order = np.argsort(counts)[::-1][:top]
    return ", ".join(f"{status_name(int(kinds[i]))} {int(counts[i])}" for i in order)


def input_line(h: dict, slot: int) -> str:
    cap = h.get("input_capture", {})
    i = slot - 1
    src = cap.get("port_sources", ["?", "?"])[i] if "port_sources" in cap else "?"
    out = f"     input sources: {src}"
    if "rows_control_module" in cap:
        out += (f", control module rows {cap['rows_control_module'][i]} "
                f"(filled {cap['rows_from_control_module'][i]})")
    if "rows_expert" in cap:
        out += f", rows_expert {cap['rows_expert'][i]}"
    ag = cap.get("control_module_agreement", [{}, {}])[i] if "control_module_agreement" in cap else {}
    if ag.get("rows_compared"):
        out += f", fim agreement {ag['all_pct']}% of {ag['rows_compared']} rows"
    return out


def summarize(m: LoadedMatch) -> str:
    h = m.header
    n = len(m.frames)
    lines = [
        f"match {h['match_id']}  started {h['started_at']}",
        f"  game {h['game_version']}  mods {h['mod_hash']}  stage {h['stage_layout']} "
        f"(id {h['stage_id']}, from {h['stage_source']})  ruleset {h['ruleset']}",
        f"  {n} frames ({h['first_frame']}..{h['last_frame']}, {h['frames_skipped']} skipped), "
        f"{h['duration_s']:.1f}s, ended by {h['end_reason']}",
    ]
    winner = h["winner_port"]
    lines.append(f"  winner: {'P' + str(winner) if winner else 'none'}"
                 + (f" (by {h['winner_source']})" if winner else ""))
    lines.append(f"  inputs: {'present' if h['inputs_present'] else 'absent (columns zero-filled)'}")
    for p in h["ports"]:
        col = m.port(p["slot"])
        x = col["pos_x"]
        lines.append(
            f"  P{p['slot']} {p['fighter_name']:<10} [{p['controller']}]  stocks {p['start_stocks']} -> "
            f"{p['final_stocks']}, damage taken {p['damage_taken']:.1f}%, "
            f"x range {x.min():.1f}..{x.max():.1f}" if n else f"  P{p['slot']} no frames"
        )
        if n:
            lines.append(f"     top statuses: {top_statuses(col)}")
            lines.append(input_line(h, p["slot"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--expect-version", default=None)
    ap.add_argument("--expect-mod-hash", default=None)
    args = ap.parse_args(argv)
    status = 0
    for path in args.files:
        try:
            m = load_match(path, args.expect_version, args.expect_mod_hash)
        except (LogRejected, OSError, KeyError, ValueError) as e:
            print(f"{path}: rejected: {e}", file=sys.stderr)
            status = 1
            continue
        print(summarize(m))
    return status


if __name__ == "__main__":
    sys.exit(main())
