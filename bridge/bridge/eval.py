"""Eval harness (SSB-12): watch N matches, score them, append one row to
eval/results.csv.

    python -m bridge.eval --games 50 --model scripted --opponent cpu9 --bot-port 2 --stage fd

Match boundaries come from matches.MatchWatcher and each match goes through
MatchLogger (version check, winner, per-frame log), so eval counts exactly
what the logger would write. Per-match detail lands in
data/eval/<run_id>.csv as each match finishes, so a crash mid-run loses
nothing. The results.csv row is only written once all N games are in.

This only watches. It doesn't send inputs or touch menus, so someone has to
press rematch between games. See eval/README.md for what fully unattended
needs. To run it in the same process as the bot (one UDP socket), build the
observers with `EvalSession(...).observers()` and hand them to the bot loop's
MatchWatcher.
"""
from __future__ import annotations

import argparse
import csv
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .kinds import kind_name
from .logger import DEFAULT_RULESET, LoggedMatch, MatchLogger, default_data_dir
from .matches import MatchWatcher
from .stages import layout_by_name
from .wire import BadPacket

REPO = Path(__file__).resolve().parents[2]
RESULTS_CSV = REPO / "eval" / "results.csv"

RESULTS_COLUMNS = [
    "date", "model", "game_version", "mod_hash", "platform", "stage", "opponent",
    "games", "wins", "stocks_taken", "avg_damage_dealt",
]
MATCH_COLUMNS = [
    "run_id", "game", "match_id", "started_at", "model", "opponent", "platform", "stage",
    "game_version", "bot_port", "bot_fighter", "opp_fighter", "frames", "duration_s",
    "end_reason", "winner_port", "win", "stocks_taken", "stocks_lost", "damage_dealt",
    "damage_taken", "log_path",
]


def match_row(m: LoggedMatch, bot_port: int, **run: Any) -> dict[str, Any]:
    """Score one logged match from the bot's side."""
    h = m.header
    bot = h["ports"][bot_port - 1]
    opp = h["ports"][2 - bot_port]
    return {
        **run,
        "match_id": h["match_id"],
        "started_at": h["started_at"],
        "stage": h["stage_layout"],
        "game_version": h["game_version"],
        "bot_port": bot_port,
        "bot_fighter": bot["fighter_name"],
        "opp_fighter": opp["fighter_name"],
        "frames": h["frame_count"],
        "duration_s": h["duration_s"],
        "end_reason": h["end_reason"],
        "winner_port": h["winner_port"] or "",
        "win": int(h["winner_port"] == bot_port),
        "stocks_taken": opp["stocks_lost"],
        "stocks_lost": bot["stocks_lost"],
        "damage_dealt": round(opp["damage_taken"], 1),
        "damage_taken": round(bot["damage_taken"], 1),
        "log_path": str(m.path) if m.path else "",
    }


def aggregate(rows: list[dict[str, Any]], **run: Any) -> dict[str, Any]:
    """One results.csv row from per-match rows. `run` supplies date, model, etc."""
    games = len(rows)
    stages = sorted({r["stage"] for r in rows})
    versions = sorted({r["game_version"] for r in rows})
    out = {
        **run,
        "game_version": versions[0] if len(versions) == 1 else "mixed",
        "stage": stages[0] if len(stages) == 1 else "mixed",
        "games": games,
        "wins": sum(int(r["win"]) for r in rows),
        "stocks_taken": sum(int(r["stocks_taken"]) for r in rows),
        "avg_damage_dealt": round(sum(float(r["damage_dealt"]) for r in rows) / games, 1) if games else 0.0,
    }
    return {k: out.get(k, "") for k in RESULTS_COLUMNS}


def ensure_header(path: Path, columns: list[str]) -> None:
    """Create the CSV with a header, or upgrade an older header in place."""
    if not path.exists() or path.stat().st_size == 0:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as f:
            csv.writer(f).writerow(columns)
        return
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
        f.seek(0)
        old = next(csv.reader(f))
    if old == columns:
        return
    missing = [c for c in old if c not in columns]
    if missing:
        raise ValueError(f"{path} has columns this version doesn't know: {missing}")
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, columns, restval="")
        w.writeheader()
        w.writerows(rows)


def append_row(path: Path, columns: list[str], row: dict[str, Any]) -> None:
    ensure_header(path, columns)
    with path.open("a", newline="") as f:
        csv.DictWriter(f, columns, restval="", extrasaction="ignore").writerow(row)


@dataclass
class EvalSession:
    games: int = 50
    model: str = "unknown"
    opponent: str = "cpu9"
    bot_port: int = 2
    platform: str = "switch"
    stage: str = "fd"
    mod_hash: str = "unset"
    expect_version: str | None = "13.0.4"
    min_seconds: float = 10.0
    results_csv: Path = RESULTS_CSV
    data_dir: Path | None = None
    write_logs: bool = True
    echo: Callable[[str], None] = print
    rows: list[dict[str, Any]] = field(default_factory=list)
    skipped: int = 0
    result: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.bot_port not in (1, 2):
            raise ValueError("bot port must be 1 or 2")
        self.stage = layout_by_name(self.stage).name
        self.data_dir = Path(self.data_dir) if self.data_dir else default_data_dir()
        self.started = datetime.now(timezone.utc)
        self.run_id = self.started.strftime("%Y%m%d-%H%M%S") + f"-{self.model}"
        self.match_csv = self.data_dir / "eval" / f"{self.run_id}.csv"
        controllers = ["", ""]
        controllers[self.bot_port - 1] = f"bot:{self.model}"
        controllers[2 - self.bot_port] = "cpu:9" if self.opponent == "cpu9" else self.opponent
        self.logger = MatchLogger(
            out_dir=self.data_dir / "matches", expect_version=self.expect_version,
            mod_hash=self.mod_hash, stage=self.stage, ruleset=dict(DEFAULT_RULESET),
            controllers=tuple(controllers), min_frames=int(self.min_seconds * 60),
            write=self.write_logs, on_logged=self._on_logged, echo=self.echo,
        )

    def observers(self) -> list[object]:
        return [self.logger]

    @property
    def done(self) -> bool:
        return len(self.rows) >= self.games

    def _run_fields(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "model": self.model, "opponent": self.opponent,
            "platform": self.platform,
        }

    def _on_logged(self, m: LoggedMatch) -> None:
        if self.done:
            return
        if not m.accepted:
            self.skipped += 1
            self.echo(f"[eval] not counting match {m.match_id}: {m.reason}")
            return
        if not m.header["complete"]:
            self.skipped += 1
            self.echo(f"[eval] not counting match {m.match_id}: cut off ({m.header['end_reason']})")
            return
        kinds = [p["fighter_name"] for p in m.header["ports"]]
        if kinds[self.bot_port - 1] != kind_name(0x2D):
            self.echo(f"[eval] heads up: port {self.bot_port} is {kinds[self.bot_port - 1]}, not robot")
        row = match_row(m, self.bot_port, game=len(self.rows) + 1, **self._run_fields())
        self.rows.append(row)
        append_row(self.match_csv, MATCH_COLUMNS, row)
        wins = sum(r["win"] for r in self.rows)
        verdict = "WIN" if row["win"] else ("LOSS" if row["winner_port"] else "NO RESULT")
        self.echo(
            f"[eval] game {len(self.rows)}/{self.games}: {verdict}, took {row['stocks_taken']} stocks, "
            f"dealt {row['damage_dealt']}%. Running {wins}/{len(self.rows)}"
        )
        if self.done:
            self.finish()
        else:
            self.echo("[eval] press rematch for the next game")

    def finish(self) -> dict[str, Any] | None:
        """Write the results.csv row. Only for a full run; returns it."""
        if not self.done or self.result is not None:
            return self.result
        self.result = aggregate(
            self.rows, date=self.started.date().isoformat(), model=self.model,
            mod_hash=self.mod_hash, platform=self.platform, opponent=self.opponent,
        )
        append_row(self.results_csv, RESULTS_COLUMNS, self.result)
        r = self.result
        self.echo(
            f"[eval] done: {r['wins']}/{r['games']} wins ({100 * r['wins'] / r['games']:.0f}%), "
            f"{r['stocks_taken']} stocks taken, {r['avg_damage_dealt']}% avg damage. "
            f"Row added to {self.results_csv}, per-match detail in {self.match_csv}"
        )
        return self.result


def watch(session: EvalSession, port: int, idle_timeout: float | None = None) -> int:
    """Own the UDP socket and feed the session until it has all its games."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.2)
    watcher = MatchWatcher(session.observers())
    session.echo(f"[eval] {session.run_id}: waiting for {session.games} games on UDP :{port}, "
                 f"bot on P{session.bot_port}, stage {session.stage}")
    last_packet = time.monotonic()
    try:
        while not session.done:
            try:
                buf, _ = sock.recvfrom(2048)
            except socket.timeout:
                buf = None
            now = time.monotonic()
            if buf is not None:
                last_packet = now
                try:
                    watcher.packet(buf, now)
                except BadPacket:
                    pass
            watcher.poll(now)
            if idle_timeout and now - last_packet > idle_timeout:
                session.echo(f"[eval] nothing from the Switch for {idle_timeout:.0f}s, stopping")
                break
    except KeyboardInterrupt:
        session.echo("[eval] interrupted")
    finally:
        sock.close()
    if not session.done:
        session.echo(
            f"[eval] stopped at {len(session.rows)}/{session.games} games, no results.csv row written. "
            f"Per-match detail is in {session.match_csv}"
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Watch N matches and log eval results (SSB-12)")
    ap.add_argument("--games", type=int, default=50)
    ap.add_argument("--model", required=True, help="name of the bot under test, e.g. scripted")
    ap.add_argument("--opponent", default="cpu9")
    ap.add_argument("--bot-port", type=int, default=2, choices=(1, 2))
    ap.add_argument("--platform", default="switch", choices=("switch", "emulator"))
    ap.add_argument("--stage", default="fd", help="battlefield, fd or omega (until stage_id is streamed)")
    ap.add_argument("--mod-hash", default="unset")
    ap.add_argument("--expect-version", default="13.0.4")
    ap.add_argument("--min-seconds", type=float, default=10.0, help="ignore matches shorter than this")
    ap.add_argument("--port", type=int, default=4243)
    ap.add_argument("--results", type=Path, default=RESULTS_CSV)
    ap.add_argument("--data-dir", type=Path, default=None, help="default <repo>/data")
    ap.add_argument("--no-log", action="store_true", help="don't write per-frame match logs")
    ap.add_argument("--idle-timeout", type=float, default=None, help="give up after this long with no packets")
    args = ap.parse_args(argv)
    session = EvalSession(
        games=args.games, model=args.model, opponent=args.opponent, bot_port=args.bot_port,
        platform=args.platform, stage=args.stage, mod_hash=args.mod_hash,
        expect_version=args.expect_version, min_seconds=args.min_seconds,
        results_csv=args.results, data_dir=args.data_dir, write_logs=not args.no_log,
    )
    return watch(session, args.port, args.idle_timeout)


if __name__ == "__main__":
    sys.exit(main())
