"""Per-frame match logger (SSB-9). Format is in docs/logging-schema.md.

One `.npz` per match plus a `.json` sidecar with the same header, under
`data/matches/`. Plug a `MatchLogger` into a `MatchWatcher` (see matches.py)
and it writes a file every time a match ends, unless the game version is
wrong, in which case it prints why and writes nothing.

    from bridge.logger import load_match
    m = load_match("data/matches/20261015-201500-a1b2c3.npz")
    m.players["damage"][:, 1]   # port 2 damage, every frame
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .kinds import kind_name
from .matches import (
    END_INTERRUPTED,
    MatchEnded,
    MatchStarted,
    MatchSummary,
    VersionGate,
    fighters,
)
from .protocol import Fighter, Frame, Inputs
from .stages import layout_name, stage_layout

SCHEMA_VERSION = 1
N_PORTS = 2

# One row per frame.
FRAME_DTYPE = np.dtype([
    ("frame", "<u4"),      # game frame counter from the plugin
    ("seq", "<u4"),        # UDP packet seq
    ("recv_t", "<f8"),     # seconds since the first frame of the match, PC clock
    ("stage_id", "<i4"),
])

# One row per frame per port: shape (n_frames, 2), column 0 is port 1.
PLAYER_DTYPE = np.dtype([
    ("fighter_kind", "<i4"),
    ("pos_x", "<f4"),
    ("pos_y", "<f4"),
    ("vel_x", "<f4"),
    ("vel_y", "<f4"),
    ("damage", "<f4"),
    ("shield_hp", "<f4"),
    ("stocks", "<i4"),
    ("status_kind", "<i4"),
    ("motion_kind", "<u8"),
    ("anim_frame", "<f4"),
    ("facing", "<i4"),
    ("jumps_left", "<i4"),
    ("flags", "<u4"),
    # Inputs that frame. Zero with input_valid = 0 until inputs are captured.
    ("stick_x", "i1"),
    ("stick_y", "i1"),
    ("cstick_x", "i1"),
    ("cstick_y", "i1"),
    ("buttons", "<u4"),
    ("input_valid", "u1"),
])
INPUT_FIELDS = ("stick_x", "stick_y", "cstick_x", "cstick_y", "buttons")

STATE_FIELDS = [
    "fighter_kind", "pos_x", "pos_y", "vel_x", "vel_y", "damage", "shield_hp", "stocks",
    "status_kind", "motion_kind", "anim_frame", "facing", "jumps_left", "flags",
]

DEFAULT_RULESET = {"stocks": 3, "items": False, "time_limit_min": None}
DEAD_STATUS = 181  # FIGHTER_STATUS_KIND_DEAD


def _action_encoder() -> Callable | None:
    """`training.actions.encode_arrays`, used to compare two input sources in
    action-class space. The bridge tests run from `bridge/`, where `training`
    isn't importable, so add the repo root if it's there. None if it can't be
    found (the agreement stats are then reported as null)."""
    try:
        from training.actions import encode_arrays
        return encode_arrays
    except ImportError:
        root = str(Path(__file__).resolve().parents[2])
        if (Path(root) / "training" / "actions.py").is_file() and root not in sys.path:
            sys.path.append(root)
            try:
                from training.actions import encode_arrays
                return encode_arrays
            except ImportError:
                return None
        return None


def agreement(a: np.ndarray, b: np.ndarray, encode: Callable | None) -> dict[str, Any]:
    """How often two input records for the same port and frames fall in the
    same action class. `a` and `b` are PLAYER_DTYPE rows (same length)."""
    n = len(a)
    out: dict[str, Any] = {"rows_compared": n}
    if encode is None:
        return {**out, "main": None, "cstick": None, "buttons": None, "all": None, "all_pct": None}
    if n == 0:
        return {**out, "main": 0, "cstick": 0, "buttons": 0, "all": 0, "all_pct": None}
    ea = encode(*(a[k] for k in INPUT_FIELDS))
    eb = encode(*(b[k] for k in INPUT_FIELDS))
    same = [x == y for x, y in zip(ea, eb)]
    out.update(main=int(same[0].sum()), cstick=int(same[1].sum()), buttons=int(same[2].sum()))
    both = same[0] & same[1] & same[2]
    out.update({"all": int(both.sum()), "all_pct": round(100.0 * float(both.mean()), 2)})
    return out


class LogRejected(ValueError):
    """A log file whose game version or mod hash doesn't match what you asked for."""


def default_data_dir() -> Path:
    """`<repo>/data` when running from a checkout, else `./data`."""
    repo = Path(__file__).resolve().parents[2]
    return repo / "data" if (repo / "eval").is_dir() and (repo / "bridge").is_dir() else Path("data")


def decide_winner(final: Frame) -> tuple[int | None, str | None]:
    """(winner port, how we know). Stocks first; if those tie (the last stock
    loss can land after the plugin stops sending), fall back to whoever is in
    DEAD on the final frame."""
    a, b = fighters(final)
    if a.stocks >= 0 and b.stocks >= 0 and a.stocks != b.stocks:
        return (1 if a.stocks > b.stocks else 2), "stocks"
    dead = [a.status_kind == DEAD_STATUS, b.status_kind == DEAD_STATUS]
    if dead == [True, False]:
        return 2, "dead_status"
    if dead == [False, True]:
        return 1, "dead_status"
    return None, None


@dataclass
class LoggedMatch:
    """Result of one match end, written or not."""

    match_id: str
    accepted: bool       # passed the version check (and min_frames)
    reason: str
    header: dict[str, Any]
    summary: MatchSummary
    path: Path | None = None  # set when the file was written


class MatchLogger:
    """Collects frames for the current match and writes them on match end.

    `controllers` names who's on each port: a human name, `bot:<model>`, or
    `cpu:9`. `stage` is the fallback layout for stage ids that aren't Battlefield or FD/Omega.
    """

    def __init__(
        self,
        out_dir: Path | str | None = None,
        expect_version: str | None = "13.0.4",
        mod_hash: str = "unset",
        stage: str = "fd",
        ruleset: dict[str, Any] | None = None,
        controllers: tuple[str, str] = ("unknown", "unknown"),
        min_frames: int = 1,
        write: bool = True,
        on_logged: Callable[[LoggedMatch], None] | None = None,
        echo: Callable[[str], None] = print,
    ) -> None:
        self.out_dir = Path(out_dir) if out_dir is not None else default_data_dir() / "matches"
        self.gate = VersionGate(expect_version)
        self.mod_hash = mod_hash
        self.stage = stage
        self.ruleset = dict(ruleset or DEFAULT_RULESET)
        self.controllers = tuple(controllers)
        self.min_frames = min_frames
        self.write = write  # False: track and check matches but don't write files
        self.on_logged = on_logged
        self.echo = echo
        self.last: LoggedMatch | None = None
        self.control = None  # latest wire.ControlStatus, for the input mapping in the header
        self._reset()

    def _reset(self) -> None:
        self._frames: list[Frame] = []
        self._seqs: list[int] = []
        self._times: list[float] = []
        self._inputs: dict[tuple[int, int], Inputs] = {}  # (port, frame) -> inputs, from acks
        self._captured: list[tuple[Inputs | None, Inputs | None]] = []  # per row, port order, from the plugin
        self._cm: list[tuple[Inputs | None, Inputs | None]] = []  # per row, ControlModule inputs (v4)
        self._summary: MatchSummary | None = None
        self._match_id: str | None = None
        self._started_at: datetime | None = None
        self._t0 = 0.0

    @property
    def in_match(self) -> bool:
        return self._match_id is not None

    # MatchWatcher observer hooks

    def on_heartbeat(self, hb) -> None:
        self.gate.heartbeat(hb.game_version)

    def on_control_status(self, st) -> None:
        self.control = st

    def on_match_start(self, ev: MatchStarted) -> None:
        self._reset()
        self.gate.start_match()
        self._started_at = datetime.now(timezone.utc)
        self._match_id = self._started_at.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self._summary = MatchSummary(ev.first)
        self._t0 = ev.now

    def on_frame(self, frame: Frame, seq: int, now: float) -> None:
        if not self.in_match:
            return
        self._frames.append(frame)
        self._seqs.append(seq)
        self._times.append(now - self._t0)
        self._captured.append(frame.inputs)
        self._cm.append(frame.cm_inputs)
        self._summary.add(frame)

    def record_inputs(self, port: int, frame: int, inputs: Inputs) -> None:
        """Inputs a port used on a game frame (1-based port), from outside the
        state stream. bridge.run calls this from the plugin's acks with the
        frame the input took effect on (`applied_frame`). Inputs the plugin
        captured itself (in the state packet) win over these when both exist;
        the header counts any disagreement."""
        self._inputs[(port, frame)] = inputs

    def on_match_end(self, ev: MatchEnded) -> LoggedMatch | None:
        if not self.in_match:
            return None
        ok, why, version = self.gate.verdict()
        if ok and len(self._frames) < self.min_frames:
            ok, why = False, f"only {len(self._frames)} frames (min {self.min_frames})"
        frames, players, capture = self._arrays()
        header = self._header(ev, version, players, capture)
        result = LoggedMatch(self._match_id, ok, why, header, self._summary)
        if ok and self.write:
            result.path = self._write(header, frames, players)
            self.echo(f"[log] wrote {result.path} ({header['frame_count']} frames, {header['end_reason']})")
        elif not ok:
            self.echo(f"[log] NOT writing match {self._match_id}: {why}")
        self.last = result
        self._reset()
        if self.on_logged is not None:
            self.on_logged(result)
        return result

    # building the file

    def _input_mapping(self) -> dict[str, Any]:
        """Which final input mapping player_idx was logged as which port.
        player_idx = port - 1 was confirmed on hardware 2026-10-16 (it follows
        the port, not the controller), so the default mapping is verified;
        a fim_player_idx override isn't."""
        st = self.control
        if st is None:
            return {"source": "none", "bot_port": None, "player_idx_by_port": None,
                    "fim_override": None, "verified": False}
        return {"source": "control_status", "bot_port": st.bot_port,
                "player_idx_by_port": st.player_idx_by_port(), "fim_override": st.fim_override,
                "verified": not st.fim_override}

    def _header(self, ev: MatchEnded, version: str | None, players: np.ndarray,
                capture: dict[str, Any]) -> dict[str, Any]:
        s = self._summary
        final = s.final or s.first
        winner, winner_source = decide_winner(final)
        has_inputs = [bool(players["input_valid"][:, i].any()) for i in range(N_PORTS)]
        stage_id = final.stage_id
        frames = [f.frame for f in self._frames]
        skipped = sum(max(0, b - a - 1) for a, b in zip(frames, frames[1:]))
        return {
            "schema_version": SCHEMA_VERSION,
            "match_id": self._match_id,
            "started_at": self._started_at.isoformat(timespec="seconds"),
            "ended_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "game_version": version,
            "mod_hash": self.mod_hash,
            "stage_id": stage_id,
            "stage_layout": layout_name(stage_id, self.stage),
            "stage_source": "stage_id" if stage_layout(stage_id) else "flag",
            "ruleset": self.ruleset,
            "ports": [
                {
                    "slot": i + 1,
                    "fighter_kind": f.fighter_kind,
                    "fighter_name": kind_name(f.fighter_kind),
                    "controller": self.controllers[i],
                    "start_stocks": s.start_stocks[i],
                    "final_stocks": f.stocks,
                    "final_damage": round(float(f.damage), 2),
                    "stocks_lost": s.stocks_lost[i],
                    "damage_taken": round(float(s.damage_taken[i]), 2),
                    "inputs_present": has_inputs[i],
                }
                for i, f in enumerate(fighters(final))
            ],
            "inputs_present": any(has_inputs),
            "input_capture": {"mapping": self._input_mapping(), **capture},
            "frame_count": len(self._frames),
            "first_frame": frames[0] if frames else None,
            "last_frame": frames[-1] if frames else None,
            "frames_skipped": skipped,
            "duration_s": round(self._times[-1], 3) if self._times else 0.0,
            "end_reason": ev.reason,
            "complete": ev.reason != END_INTERRUPTED,
            "winner_port": winner,
            "winner_source": winner_source,
        }

    def _arrays(self) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
        n = len(self._frames)
        frames = np.zeros(n, dtype=FRAME_DTYPE)
        frames["frame"] = [f.frame for f in self._frames]
        frames["seq"] = self._seqs
        frames["recv_t"] = self._times
        frames["stage_id"] = [f.stage_id for f in self._frames]
        players = np.zeros((n, N_PORTS), dtype=PLAYER_DTYPE)
        for port in range(N_PORTS):
            col = [fighters(f)[port] for f in self._frames]
            for name in STATE_FIELDS:
                players[name][:, port] = [getattr(x, name) for x in col]
        # 1. inputs from acks (the bot's, on the frame they took effect)
        row_of = {int(fr): i for i, fr in enumerate(frames["frame"])}
        from_acks = [0, 0]
        acked: dict[tuple[int, int], Inputs] = {}
        for (port, fr), inp in self._inputs.items():
            row = row_of.get(fr)
            if row is None or port not in (1, 2):
                continue
            acked[(row, port - 1)] = inp
            for name in INPUT_FIELDS:
                players[name][row, port - 1] = getattr(inp, name)
            players["input_valid"][row, port - 1] = 1
            from_acks[port - 1] += 1
        # 2. inputs the plugin captured at the final input mapping ("fim";
        # ground truth for human and bot ports: what the fighter read)
        captured = [0, 0]
        mismatches = 0
        fim_mask = np.zeros((n, N_PORTS), dtype=bool)
        for row, pair in enumerate(self._captured):
            for port, inp in enumerate(pair):
                if inp is None:
                    continue
                if (row, port) in acked and acked[(row, port)] != inp:
                    mismatches += 1
                for name in INPUT_FIELDS:
                    players[name][row, port] = getattr(inp, name)
                players["input_valid"][row, port] = 1
                fim_mask[row, port] = True
                captured[port] += 1
        # 3. ControlModule inputs (v4): the fighter's own read of its
        # controller. Only source for CPU ports. Where a port already has
        # inputs from 1 or 2 those win; the two are compared instead.
        cm_arr = np.zeros((n, N_PORTS), dtype=PLAYER_DTYPE)
        cm_mask = np.zeros((n, N_PORTS), dtype=bool)
        for row, pair in enumerate(self._cm):
            for port, inp in enumerate(pair):
                if inp is None:
                    continue
                for name in INPUT_FIELDS:
                    cm_arr[name][row, port] = getattr(inp, name)
                cm_mask[row, port] = True
        encode = _action_encoder()
        filled_cm = [0, 0]
        agree = []
        for port in range(N_PORTS):
            fill = cm_mask[:, port] & (players["input_valid"][:, port] == 0)
            for name in INPUT_FIELDS:
                players[name][fill, port] = cm_arr[name][fill, port]
            players["input_valid"][fill, port] = 1
            filled_cm[port] = int(fill.sum())
            both = cm_mask[:, port] & fim_mask[:, port]
            agree.append(agreement(players[both][:, port], cm_arr[both][:, port], encode))
        sources = []
        for port in range(N_PORTS):
            used = [name for name, k in (("ack", from_acks[port]), ("fim", captured[port]),
                                         ("control_module", filled_cm[port])) if k]
            sources.append("none" if not used else used[0] if len(used) == 1 else "+".join(used))
        capture = {
            "rows_captured": captured,
            "rows_from_acks": from_acks,
            # acked input vs what the plugin captured on the same frame. Should
            # be 0; anything else means the frame alignment is off.
            "ack_capture_mismatches": mismatches,
            # v4. Rows where the plugin sent a ControlModule read, and rows of
            # input_valid that exist only because of it (no ack, no capture).
            "rows_control_module": [int(cm_mask[:, p].sum()) for p in range(N_PORTS)],
            "rows_from_control_module": filled_cm,
            # Which source filled each port's rows: "fim" (final input
            # mapping capture), "ack", "control_module", joined with "+" if more
            # than one, "none" if no inputs at all.
            "port_sources": sources,
            # Rows where both fim and control module exist, compared after
            # encoding to training/actions.py classes. Per port.
            "control_module_agreement": agree,
        }
        return frames, players, capture

    def _write(self, header: dict[str, Any], frames: np.ndarray, players: np.ndarray) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{header['match_id']}.npz"
        tmp = path.with_suffix(".tmp.npz")
        text = json.dumps(header, indent=2)
        np.savez_compressed(tmp, header=np.array(text), frames=frames, players=players)
        os.replace(tmp, path)
        path.with_suffix(".json").write_text(text + "\n")
        return path


@dataclass
class LoadedMatch:
    header: dict[str, Any]
    frames: np.ndarray   # FRAME_DTYPE, shape (n,)
    players: np.ndarray  # PLAYER_DTYPE, shape (n, 2)

    def port(self, slot: int) -> np.ndarray:
        """All rows for one port (1-based)."""
        return self.players[:, slot - 1]


def load_match(
    path: Path | str,
    expect_version: str | None = None,
    expect_mod_hash: str | None = None,
) -> LoadedMatch:
    """Load a match. Pass expect_* to reject logs from other builds (raises LogRejected)."""
    path = Path(path)
    if path.suffix == ".json":
        path = path.with_suffix(".npz")
    with np.load(path, allow_pickle=False) as z:
        header = json.loads(str(z["header"]))
        frames, players = z["frames"], z["players"]
    if header.get("schema_version") != SCHEMA_VERSION:
        raise LogRejected(f"{path}: schema v{header.get('schema_version')}, expected v{SCHEMA_VERSION}")
    if expect_version is not None and header.get("game_version") != expect_version:
        raise LogRejected(f"{path}: game version {header.get('game_version')}, expected {expect_version}")
    if expect_mod_hash is not None and header.get("mod_hash") != expect_mod_hash:
        raise LogRejected(f"{path}: mod hash {header.get('mod_hash')}, expected {expect_mod_hash}")
    return LoadedMatch(header, frames, players)


def frame_at(m: LoadedMatch, row: int) -> Frame:
    """Rebuild a protocol Frame from one logged row (handy for replaying into a policy)."""
    fs = [Fighter(**{k: m.players[row, p][k].item() for k in STATE_FIELDS}) for p in range(N_PORTS)]
    ins = tuple(
        Inputs(*(m.players[row, p][k].item() for k in INPUT_FIELDS)) if m.players[row, p]["input_valid"] else None
        for p in range(N_PORTS)
    )
    return Frame(int(m.frames["frame"][row]), int(m.frames["stage_id"][row]), fs[0], fs[1], inputs=ins)

