"""Feature pipeline (SSB-14). Logged matches -> training tensors, and the same
features live at play time.

One core, two front doors:

- `window_features(...)` is the only code that turns state into features.
  It works on arrays shaped like the logger's (`frames["frame"]`,
  `frames["stage_id"]`, `players` with `PLAYER_DTYPE`) and a batch of "now"
  rows.
- Live: `live_features(history, prev_inputs, delay, cfg)` packs the
  `Sequence[Frame]` a Policy gets into those same arrays (`frames_to_arrays`)
  and calls the core for one row.
- Training: `BCDataset.batch(...)` calls the core on the concatenated logged
  arrays for many rows at once.

`tests/test_features.py` checks the two give identical features, bit for bit,
for the same frames (dropped frames and match start included).

## What one sample looks like

A sample is "the bot sees game frame t and picks inputs that land on frame
t + d" (d = input delay, see train_bc.py). Its features:

Window: the last `window` game frames (t - window + 1 .. t), by frame number,
not by received row. For each slot:

- per fighter (bot first, then opponent), 15 floats: x and y relative to the
  stage center and ground, vel x/y, damage, shield, stocks, anim_frame,
  facing, jumps_left, airborne, hitstun, invincible, distance to the nearest
  ledge, offstage flag
- 5 shared floats: dx, dy, distance (opponent minus bot), `pad` (1 if the
  slot is before the first frame we have, i.e. match start), `age` (how many
  frames old the data in this slot is, 0 normally, >0 when the frame was
  dropped and we reused the last one we got)
- categoricals: bot status_kind, opponent status_kind

Dropped frames: a missing slot reuses the newest earlier frame we have
(forward fill) and says so through `age`. Nothing is interpolated.
Match start: slots before the first frame copy the first frame with pad = 1.
Only the last `history_len` received rows are ever looked at, because that's
all a live Policy gets (bridge.policy.History keeps 16), and the window stops
at a frame counter reset (new match).

Static: `delay / 8` (float), fighter kind for both, stage layout
(unknown / battlefield / fd), and the bot's own last `prev_inputs` inputs as
action classes (main, c-stick, buttons each), oldest first, with an extra
"unknown" class when we don't know them (not logged, or start of play).

"Own last inputs" means the last K inputs the bot already committed before
this decision. With delay d those are the inputs for frames t+d-1 .. t+d-K,
so that's what the dataset path looks up. Live, it's just the last K things
the policy returned.

Bot perspective: `bot_port` picks which logged column is "me". Positions are
relative to the layout's center (both v1 layouts are centered on 0, but this
keeps it right if that changes).

Not mirrored. We considered flipping x so the bot always faces right, but
facing changes inside a window (turnarounds), and mirroring would also mean
mirroring the action labels and prev inputs. Both v1 stages are symmetric, so
mirroring is better done later as data augmentation (flip x features and
stick_x labels together) than baked into the features.

Scales are fixed constants (no fitted stats), so there's nothing to save
next to a checkpoint except `FeatureConfig`.
"""
from __future__ import annotations

import hashlib
import glob
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from bridge.logger import EXPERT_FIELDS, PLAYER_DTYPE, STATE_FIELDS, LoadedMatch, load_match
from bridge.matches import fighters
from bridge.protocol import Frame, Inputs
from bridge.stages import LAYOUT_BATTLEFIELD, LAYOUT_FD, LAYOUTS, stage_layout

from . import actions as A

EXPECT_VERSION = "13.0.4"

# Embedding vocab sizes. Index 0 is always "unknown / out of range".
STATUS_VOCAB = 1024   # status_kind + 1; R.O.B.'s own statuses sit above the common ~470
FIGHTER_VOCAB = 128   # fighter_kind + 1
LAYOUT_INDEX = {None: 0, LAYOUT_BATTLEFIELD: 1, LAYOUT_FD: 2}
LAYOUT_VOCAB = len(LAYOUT_INDEX)
# Prev-input classes: the action classes plus one extra "unknown" at the end.
PREV_UNKNOWN = (A.N_MAIN, A.N_CSTICK, A.N_BUTTONS)
PREV_VOCAB = (A.N_MAIN + 1, A.N_CSTICK + 1, A.N_BUTTONS + 1)

PER_FIGHTER = 15
SHARED = 5
PER_SLOT = 2 * PER_FIGHTER + SHARED
DELAY_SCALE = 8.0
MAX_AGE = 30


@dataclass(frozen=True)
class FeatureConfig:
    window: int = 12        # game frames of state history, 8 to 16
    prev_inputs: int = 4    # bot's own last K inputs fed back in
    bot_port: int = 1       # which logged column is the bot (1 or 2)
    history_len: int = 16   # rows a live Policy gets (bridge.policy.History length)

    def __post_init__(self) -> None:
        if not 8 <= self.window <= 16:
            raise ValueError(f"window must be 8..16, got {self.window}")
        if self.window > self.history_len:
            raise ValueError("window can't be longer than history_len")
        if self.bot_port not in (1, 2):
            raise ValueError("bot_port must be 1 or 2")
        if self.prev_inputs < 0:
            raise ValueError("prev_inputs must be >= 0")

    @property
    def n_cont(self) -> int:
        return self.window * PER_SLOT + 1

    @property
    def n_cat(self) -> int:
        return 2 * self.window + 3 + 3 * self.prev_inputs

    def cat_tables(self) -> list[str]:
        """Which embedding table each categorical column uses, in order."""
        cols = ["status", "status"] * self.window + ["fighter", "fighter", "layout"]
        cols += ["prev_main", "prev_cstick", "prev_buttons"] * self.prev_inputs
        return cols

    def to_dict(self) -> dict:
        return asdict(self)


VOCABS = {
    "status": STATUS_VOCAB,
    "fighter": FIGHTER_VOCAB,
    "layout": LAYOUT_VOCAB,
    "prev_main": PREV_VOCAB[0],
    "prev_cstick": PREV_VOCAB[1],
    "prev_buttons": PREV_VOCAB[2],
}


# ---------------------------------------------------------------------------
# The core. Everything else goes through here.

_LAYOUT_CACHE: dict[int, tuple[int, float, float, float]] = {}


def _layout_of(sid: int) -> tuple[int, float, float, float]:
    """(layout index, center_x, ground_y, ledge_x) for a stage id. Unknown
    stages use center 0 / ground 0 / FD ledge so the numbers stay sane."""
    hit = _LAYOUT_CACHE.get(sid)
    if hit is None:
        name = stage_layout(sid)
        lay = LAYOUTS[name] if name else LAYOUTS[LAYOUT_FD]
        hit = _LAYOUT_CACHE[sid] = (LAYOUT_INDEX[name], lay.center_x, lay.ground_y, lay.ledge_x)
    return hit


def _layout_arrays(stage_ids: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Per stage id: layout index, center_x, ground_y, ledge_x."""
    if stage_ids.size == 1:
        uniq, inv = stage_ids.reshape(1), np.zeros(1, np.int64)
    else:
        uniq, inv = np.unique(stage_ids, return_inverse=True)
    vals = [_layout_of(int(s)) for s in uniq]
    idx = np.array([v[0] for v in vals], np.int64)
    cx, gy, lx = (np.array([v[i] for v in vals], np.float32) for i in (1, 2, 3))
    inv = inv.reshape(stage_ids.shape)
    return idx[inv], cx[inv], gy[inv], lx[inv]


def _fighter_feats(p: np.ndarray, cx: np.ndarray, gy: np.ndarray, lx: np.ndarray) -> list[np.ndarray]:
    f32 = np.float32
    x = p["pos_x"].astype(f32) - cx
    flags = p["flags"].astype(np.int64)
    edge = lx - np.abs(x)
    return [
        x / f32(100),
        (p["pos_y"].astype(f32) - gy) / f32(100),
        p["vel_x"].astype(f32) / f32(3),
        p["vel_y"].astype(f32) / f32(3),
        p["damage"].astype(f32) / f32(100),
        p["shield_hp"].astype(f32) / f32(50),
        p["stocks"].astype(f32) / f32(3),
        np.clip(p["anim_frame"].astype(f32), 0, 300) / f32(60),
        np.sign(p["facing"]).astype(f32),
        p["jumps_left"].astype(f32) / f32(3),
        (flags & 1).astype(f32),
        ((flags >> 1) & 1).astype(f32),
        ((flags >> 2) & 1).astype(f32),
        edge / f32(100),
        (edge < 0).astype(f32),
    ]


def _status_idx(s: np.ndarray) -> np.ndarray:
    s = s.astype(np.int64)
    return np.where((s >= 0) & (s < STATUS_VOCAB - 1), s + 1, 0)


def _kind_idx(k: np.ndarray) -> np.ndarray:
    k = k.astype(np.int64)
    return np.where((k >= 0) & (k < FIGHTER_VOCAB - 1), k + 1, 0)


def window_features(
    keys: np.ndarray,       # (n,) int64, increasing within a match: frame number (+ match offset)
    starts: np.ndarray,     # (n,) int64, row index where each row's match (or run) begins
    stage_ids: np.ndarray,  # (n,) int32
    players: np.ndarray,    # (n, 2) PLAYER_DTYPE (only STATE_FIELDS are read)
    ends: np.ndarray,       # (B,) rows that are "now" for each sample
    delays: np.ndarray,     # (B,) input delay d per sample
    prev_cls: np.ndarray,   # (B, K, 3) int64 action classes, oldest first, PREV_UNKNOWN for unknown
    cfg: FeatureConfig,
) -> tuple[np.ndarray, np.ndarray]:
    """Features for a batch of rows. Returns (cont (B, n_cont) float32, cat (B, n_cat) int64)."""
    ends = np.asarray(ends, np.int64)
    B, W = len(ends), cfg.window
    bi, oi = cfg.bot_port - 1, 2 - cfg.bot_port

    # Which row fills each slot: newest row with frame <= the slot's frame,
    # never older than the history a live policy has, never across a reset.
    lo = np.maximum(ends - cfg.history_len + 1, starts[ends])
    target = keys[ends][:, None] - np.arange(W - 1, -1, -1, dtype=np.int64)[None, :]
    src = np.searchsorted(keys, target, side="right") - 1
    pad = src < lo[:, None]
    src = np.where(pad, lo[:, None], src)
    age = np.where(pad, 0, np.minimum(target - keys[src], MAX_AGE)).astype(np.float32)

    layout_idx, cx, gy, lx = _layout_arrays(stage_ids[ends])
    cx, gy, lx = cx[:, None], gy[:, None], lx[:, None]
    me = players[src, bi]
    opp = players[src, oi]
    mf = _fighter_feats(me, cx, gy, lx)
    of = _fighter_feats(opp, cx, gy, lx)
    dx = of[0] - mf[0]
    dy = of[1] - mf[1]
    shared = [dx, dy, np.sqrt(dx * dx + dy * dy), pad.astype(np.float32), age / np.float32(10)]
    per_slot = np.stack(mf + of + shared, axis=-1)  # (B, W, PER_SLOT)
    delay = (np.asarray(delays, np.float32) / np.float32(DELAY_SCALE))[:, None]
    cont = np.concatenate([per_slot.reshape(B, -1), delay], axis=1).astype(np.float32)

    status = np.stack([_status_idx(me["status_kind"]), _status_idx(opp["status_kind"])], axis=-1)
    now = players[ends]
    static = np.stack([_kind_idx(now[:, bi]["fighter_kind"]), _kind_idx(now[:, oi]["fighter_kind"]), layout_idx], axis=-1)
    prev = np.asarray(prev_cls, np.int64).reshape(B, -1)
    cat = np.concatenate([status.reshape(B, -1), static, prev], axis=1)
    return cont, cat


# ---------------------------------------------------------------------------
# Live path

def frames_to_arrays(history: Sequence[Frame]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(keys, starts, stage_ids, players) for a Policy's history, same layout as a log."""
    n = len(history)
    keys = np.fromiter((f.frame for f in history), np.int64, n)
    stage_ids = np.fromiter((f.stage_id for f in history), np.int32, n)
    players = np.zeros((n, 2), PLAYER_DTYPE)
    for port in range(2):
        col = [fighters(f)[port] for f in history]
        for name in STATE_FIELDS:
            players[name][:, port] = [getattr(x, name) for x in col]
    # A frame counter that goes backwards starts a new match. live_features
    # cuts the history there first; this is only the bookkeeping.
    resets = np.flatnonzero(np.diff(keys) < 0) + 1
    start = int(resets[-1]) if len(resets) else 0
    starts = np.full(n, start, np.int64)
    return keys, starts, stage_ids, players


def prev_classes(prev_inputs: Sequence[Inputs | None] | None, k: int) -> np.ndarray:
    """(k, 3) classes for the last k inputs, oldest first. Missing -> unknown."""
    out = np.array([PREV_UNKNOWN] * k, np.int64).reshape(k, 3)
    if not k or not prev_inputs:
        return out
    recent = list(prev_inputs)[-k:]
    for j, inp in enumerate(recent):
        if inp is not None:
            c = A.encode(inp)
            out[k - len(recent) + j] = (c.main, c.cstick, c.buttons)
    return out


def live_features(
    history: Sequence[Frame],
    prev_inputs: Sequence[Inputs | None] | None,
    delay: int,
    cfg: FeatureConfig,
) -> tuple[np.ndarray, np.ndarray]:
    """Features for the newest frame in `history`: (cont (n_cont,), cat (n_cat,)).

    `prev_inputs` are the inputs the bot already committed, most recent last
    (what the policy returned on earlier frames). `delay` is the input delay
    the policy is planning around.
    """
    if not history:
        raise ValueError("empty history")
    history = list(history[-cfg.history_len:])
    # Drop anything before a frame counter reset (new match). Must happen
    # before the core: it needs keys increasing.
    for i in range(len(history) - 1, 0, -1):
        if history[i].frame < history[i - 1].frame:
            history = history[i:]
            break
    keys, starts, stage_ids, players = frames_to_arrays(history)
    cont, cat = window_features(
        keys, starts, stage_ids, players,
        np.array([len(history) - 1]), np.array([delay]),
        prev_classes(prev_inputs, cfg.prev_inputs)[None], cfg,
    )
    return cont[0], cat[0]


# ---------------------------------------------------------------------------
# Dataset path

@dataclass
class BCDataset:
    """All logged matches concatenated, plus what's needed to build samples.

    Rows are frames. A sample is (row i, delay d) with label = the bot's
    inputs on frame `frame[i] + d`, only if that frame was logged with
    input_valid = 1. `label_row[i, d]` is that row or -1.
    """

    cfg: FeatureConfig
    max_delay: int
    keys: np.ndarray        # (N,) match_index << 32 | frame
    starts: np.ndarray      # (N,) first row of each row's match
    stage_ids: np.ndarray
    players: np.ndarray
    match_index: np.ndarray  # (N,) which match (index into match_ids) each row came from
    match_ids: list[str]
    labels: np.ndarray      # (N, 3) action classes at each row, -1 where input_valid = 0
    label_row: np.ndarray   # (N, max_delay + 1)
    # DAgger (SSB-18). `labels` is always what the bot actually played (it
    # feeds the prev-input features: that is what it sees live). `targets`,
    # when set, is what the model is trained to output: the CPU AI's decision
    # on expert rows, -1 on rows of an expert-bearing log that have none,
    # `labels` elsewhere. `expert_row` marks the rows whose target is an
    # expert decision.
    targets: np.ndarray | None = None
    expert_row: np.ndarray | None = None

    @property
    def target_labels(self) -> np.ndarray:
        return self.labels if self.targets is None else self.targets

    def sample_weights(self, rows: np.ndarray, delays: np.ndarray, expert_weight: float) -> np.ndarray:
        """Loss weight per sample: `expert_weight` where the target is an expert decision, else 1."""
        w = np.ones(len(rows), np.float32)
        if self.expert_row is not None and expert_weight != 1.0:
            w[self.expert_row[self.label_row[np.asarray(rows), np.asarray(delays)]]] = expert_weight
        return w

    @property
    def n_rows(self) -> int:
        return len(self.keys)

    def sample_rows(self, matches: Iterable[int] | None = None) -> np.ndarray:
        """Rows with at least one valid delay, optionally only from some matches."""
        ok = (self.label_row >= 0).any(axis=1)
        if matches is not None:
            ok &= np.isin(self.match_index, np.fromiter(matches, np.int64))
        return np.flatnonzero(ok)

    def pick_delays(self, rows: np.ndarray, rng: np.random.Generator) -> np.ndarray:
        """A uniformly random valid delay for each row."""
        valid = self.label_row[rows] >= 0
        score = rng.random(valid.shape) + valid  # valid ones always beat invalid ones
        return np.argmax(score, axis=1)

    def _prev(self, rows: np.ndarray, delays: np.ndarray) -> np.ndarray:
        K = self.cfg.prev_inputs
        out = np.empty((len(rows), K, 3), np.int64)
        out[:] = np.array(PREV_UNKNOWN, np.int64)
        if not K:
            return out
        # inputs for frames t+d-K .. t+d-1, oldest first
        want = self.keys[rows][:, None] + delays[:, None] - np.arange(K, 0, -1)[None, :]
        pos = np.searchsorted(self.keys, want)
        pos_c = np.minimum(pos, self.n_rows - 1)
        hit = (self.keys[pos_c] == want) & (pos_c >= self.starts[rows][:, None])
        lab = self.labels[pos_c]
        good = hit & (lab[..., 0] >= 0)
        out[good] = lab[good]
        return out

    def batch(
        self, rows: np.ndarray, delays: np.ndarray,
        prev_dropout: float = 0.0, rng: np.random.Generator | None = None,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(cont, cat, targets (B, 3)) for these rows at these delays.

        `prev_dropout`: chance per sample of blanking all prev inputs to
        "unknown" (training only, see train_bc --prev-dropout)."""
        rows = np.asarray(rows, np.int64)
        delays = np.asarray(delays, np.int64)
        prev = self._prev(rows, delays)
        if prev_dropout > 0 and self.cfg.prev_inputs:
            drop = (rng or np.random.default_rng()).random(len(rows)) < prev_dropout
            prev[drop] = np.array(PREV_UNKNOWN, np.int64)
        cont, cat = window_features(
            self.keys, self.starts, self.stage_ids, self.players, rows, delays, prev, self.cfg,
        )
        tgt = self.target_labels[self.label_row[rows, delays]]
        return cont, cat, tgt

    def prev_inputs_for(self, row: int, delay: int) -> list[Inputs | None]:
        """The prev inputs the dataset path uses for (row, delay), as Inputs (for parity tests)."""
        out: list[Inputs | None] = []
        for c in self._prev(np.array([row]), np.array([delay]))[0]:
            out.append(None if c[0] == PREV_UNKNOWN[0] else A.decode(*c))
        return out


def find_matches(paths: Sequence[str | Path]) -> list[Path]:
    """Expand dirs and globs into .npz files."""
    out: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            out += sorted(p.glob("*.npz"))
        elif any(ch in str(p) for ch in "*?["):
            out += [Path(x) for x in sorted(glob.glob(str(p)))]
        else:
            out.append(p)
    return [p for p in out if not p.name.endswith(".tmp.npz")]


def _content_key(m: LoadedMatch) -> str:
    """Stable id for a match from what's in it (frame numbers and per-port
    data; not recv_t, which is wall clock timing)."""
    h = hashlib.sha1()
    h.update(np.ascontiguousarray(m.frames["frame"]).tobytes())
    h.update(np.ascontiguousarray(m.players).tobytes())
    return h.hexdigest()


def valid_input_mask(m: LoadedMatch, slot: int, include_inferred: bool = False) -> np.ndarray:
    """Rows of port `slot` (1-based) whose inputs may be used as labels.
    Recorded rows (input_valid = 1) always; rows predicted by an IDM
    (`m.inferred`, see idm.py) only with include_inferred."""
    ok = m.port(slot)["input_valid"] == 1
    if include_inferred and m.inferred is not None:
        ok = ok | m.inferred[:, slot - 1]
    return ok


def drop_derived_duplicates(loaded: list[LoadedMatch], include_inferred: bool) -> list[LoadedMatch]:
    """A labeled copy (`derived_from` in its header) holds the same frames as
    its raw log. When both are loaded keep only one: the copy if inferred rows
    are wanted, else the raw log. A copy whose raw log isn't loaded stays."""
    ids = {m.header.get("match_id") for m in loaded}
    keep = []
    for m in loaded:
        src = m.header.get("derived_from")
        if src is not None and src in ids and not include_inferred:
            continue
        keep.append(m)
    if include_inferred:
        wanted = {m.header["derived_from"] for m in keep if m.header.get("derived_from")}
        keep = [m for m in keep if m.header.get("match_id") not in wanted or m.header.get("derived_from")]
    return keep


def load_all(matches, expect_version, expect_mod_hash, include_inferred: bool) -> list[LoadedMatch]:
    """Load (paths get the version check), drop raw/labeled duplicates, and
    order by content so the train/val split doesn't depend on file names."""
    loaded = [m if isinstance(m, LoadedMatch) else load_match(m, expect_version, expect_mod_hash) for m in matches]
    if not loaded:
        raise ValueError("no matches")
    loaded = drop_derived_duplicates(loaded, include_inferred)
    loaded.sort(key=_content_key)
    return loaded


def build_dataset(
    matches: Sequence[LoadedMatch | str | Path],
    cfg: FeatureConfig = FeatureConfig(),
    max_delay: int = 4,
    expect_version: str | None = EXPECT_VERSION,
    expect_mod_hash: str | None = None,
    include_inferred: bool = False,
    expert_labels: bool = False,
) -> BCDataset:
    """Load matches (paths are loaded with expect_version, so wrong builds are
    rejected with LogRejected) and index them for training. Rows whose inputs
    were predicted by an IDM (idm.py label) are left out unless include_inferred.

    `expert_labels` (DAgger, SSB-18): on rows with expert_valid = 1 the
    training target is the CPU AI's decision instead of the played input. A
    log that has any such rows for the bot port is a bot-played match: there
    only the expert rows are targets (its other rows are the bot imitating
    itself), but its played inputs still feed the prev-input features."""
    loaded = load_all(matches, expect_version, expect_mod_hash, include_inferred)
    # (load_all ordered matches by content, not file name: ids carry a random
    # suffix and split_by_match splits by position.)
    keys, starts, sids, pls, midx, labels, ids = [], [], [], [], [], [], []
    targets, experts = [], []
    offset = 0
    for i, m in enumerate(loaded):
        n = len(m.frames)
        fr = m.frames["frame"].astype(np.int64)
        if n > 1 and (np.diff(fr) <= 0).any():
            raise ValueError(f"match {m.header.get('match_id')}: frame numbers not increasing")
        keys.append((np.int64(i) << 32) | fr)
        starts.append(np.full(n, offset, np.int64))
        sids.append(m.frames["stage_id"])
        pls.append(m.players)
        midx.append(np.full(n, i, np.int64))
        p = m.port(cfg.bot_port)
        lab = np.stack(A.encode_arrays(p["stick_x"], p["stick_y"], p["cstick_x"], p["cstick_y"], p["buttons"]), axis=1)
        lab[~valid_input_mask(m, cfg.bot_port, include_inferred)] = -1
        labels.append(lab)
        tgt = lab.copy()
        exp = np.zeros(n, bool)
        if expert_labels and (p["expert_valid"] == 1).any():
            exp = p["expert_valid"] == 1
            tgt[:] = -1
            tgt[exp] = np.stack(A.encode_arrays(*(p[k] for k in EXPERT_FIELDS)), axis=1)[exp]
        targets.append(tgt)
        experts.append(exp)
        ids.append(m.header.get("match_id", str(i)))
        offset += n
    keys = np.concatenate(keys)
    labels = np.concatenate(labels)
    targets = np.concatenate(targets)
    starts = np.concatenate(starts)
    N = len(keys)
    label_row = np.full((N, max_delay + 1), -1, np.int64)
    for d in range(max_delay + 1):
        want = keys + d
        pos = np.minimum(np.searchsorted(keys, want), N - 1)
        ok = (keys[pos] == want) & (targets[pos, 0] >= 0)
        label_row[ok, d] = pos[ok]
    return BCDataset(
        cfg=cfg, max_delay=max_delay, keys=keys, starts=starts,
        stage_ids=np.concatenate(sids), players=np.concatenate(pls),
        match_index=np.concatenate(midx), match_ids=ids, labels=labels, label_row=label_row,
        targets=targets if expert_labels else None, expert_row=np.concatenate(experts) if expert_labels else None,
    )


def split_by_match(n_matches: int, val_frac: float, seed: int) -> tuple[list[int], list[int]]:
    """Shuffle match indices and hold out a fraction. Never splits a match."""
    order = np.random.default_rng(seed).permutation(n_matches)
    n_val = max(1, round(n_matches * val_frac)) if n_matches > 1 and val_frac > 0 else 0
    return sorted(order[n_val:].tolist()), sorted(order[:n_val].tolist())
