"""Behavior cloning trainer (SSB-16).

    python -m training.train_bc data/matches --device cuda --epochs 30
    python -m training.train_bc data/matches --device cpu --model gru

Reads logged matches (rejects other game versions), builds features with
training/features.py, and trains three classification heads (main stick,
c-stick, buttons; see training/actions.py) with summed cross-entropy.

## Input delay, exactly

Inputs the PC sends land some frames after the state they answer (SSB-7
measures the worst case). So we train the model to answer "given state up to
frame t, what will the player be doing on frame t + d?":

- For each sample row i (game frame t = frame[i]) and each epoch, draw d
  uniformly from the delays in 0..--max-delay that have a label: frame
  t + d must be in the log (not dropped) with input_valid = 1 for the bot.
- Label = the bot's inputs on frame t + d, encoded to action classes.
- Features = the window ending at frame t (nothing after t is ever seen),
  plus d / 8 as an input, plus the bot's inputs on frames t+d-K .. t+d-1
  (the K inputs it would already have committed by then).
- At play time, set d to the measured delay (SCPU_DELAY) and the policy's
  output is applied on frame t + d.

Prev-input dropout (`--prev-dropout`, default 0.5): each training sample
has a 50% chance of all its "own last inputs" set to unknown. Without it the
model learns to copy its previous input (that's right on almost every frame
of a log), and live, where those inputs are its own guesses, it locks in
and never changes its mind.

Rows with no valid delay at all are never used as samples. Rows with
input_valid = 0 still provide state history for their neighbors.

Validation is split by match (`--val-frac`, seeded), never by frame, and
uses a fixed delay per sample (drawn once from the seed) so numbers are
comparable across epochs. The checkpoint with the best val loss is
`checkpoints/<run>/best.pt`, the last epoch is `last.pt`.
"""
from __future__ import annotations

import argparse
import json
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from . import actions as A
from .features import (
    EXPECT_VERSION,
    PER_SLOT,
    VOCABS,
    BCDataset,
    FeatureConfig,
    build_dataset,
    find_matches,
    split_by_match,
)

HEADS = ("main", "cstick", "buttons")
REPO = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ModelConfig:
    kind: str = "mlp"         # mlp or gru
    hidden: int = 256
    layers: int = 2
    emb_dim: int = 8          # size of each categorical embedding
    dropout: float = 0.1
    n_main: int = A.N_MAIN
    n_cstick: int = A.N_CSTICK
    n_buttons: int = A.N_BUTTONS


class BCNet(nn.Module):
    """Embeddings for the categoricals, then either:

    - mlp: everything flattened (all window slots side by side) into an MLP.
    - gru: one GRU step per window slot (that slot's floats + its two status
      embeddings), final hidden state concatenated with the static stuff
      (delay, fighter kinds, layout, prev inputs) into a small MLP.

    Same inputs and outputs either way, so bc_policy doesn't care which.
    """

    def __init__(self, fcfg: FeatureConfig, mcfg: ModelConfig) -> None:
        super().__init__()
        self.fcfg, self.mcfg = fcfg, mcfg
        tables = fcfg.cat_tables()
        # One fused table: each vocab gets a block of rows, columns that share
        # a vocab (all the status columns, say) share its block. One lookup
        # per forward instead of one per table, which matters at batch 1.
        names = sorted(set(tables))
        start, base = {}, 0
        for n in names:
            start[n] = base
            base += VOCABS[n]
        self.emb = nn.Embedding(base, mcfg.emb_dim)
        self.register_buffer("cat_offset", torch.tensor([start[t] for t in tables], dtype=torch.long), persistent=False)
        E, W = mcfg.emb_dim, fcfg.window
        if mcfg.kind == "mlp":
            d_in = fcfg.n_cont + len(tables) * E
            self.trunk = _mlp(d_in, mcfg.hidden, mcfg.layers, mcfg.dropout)
        elif mcfg.kind == "gru":
            self.gru = nn.GRU(PER_SLOT + 2 * E, mcfg.hidden, batch_first=True)
            d_static = 1 + (len(tables) - 2 * W) * E
            self.trunk = _mlp(mcfg.hidden + d_static, mcfg.hidden, max(1, mcfg.layers - 1), mcfg.dropout)
        else:
            raise ValueError(f"unknown model kind {mcfg.kind!r}")
        self.heads = nn.ModuleDict({
            "main": nn.Linear(mcfg.hidden, mcfg.n_main),
            "cstick": nn.Linear(mcfg.hidden, mcfg.n_cstick),
            "buttons": nn.Linear(mcfg.hidden, mcfg.n_buttons),
        })

    def _embed(self, cat: torch.Tensor) -> torch.Tensor:
        return self.emb(cat + self.cat_offset)  # (B, n_cat, E)

    def forward(self, cont: torch.Tensor, cat: torch.Tensor) -> dict[str, torch.Tensor]:
        e = self._embed(cat)
        B, W = cont.shape[0], self.fcfg.window
        if self.mcfg.kind == "mlp":
            h = self.trunk(torch.cat([cont, e.flatten(1)], dim=1))
        else:
            slots = cont[:, : W * PER_SLOT].reshape(B, W, PER_SLOT)
            status = e[:, : 2 * W].reshape(B, W, -1)
            _, hn = self.gru(torch.cat([slots, status], dim=2))
            static = torch.cat([cont[:, W * PER_SLOT:], e[:, 2 * W:].flatten(1)], dim=1)
            h = self.trunk(torch.cat([hn[-1], static], dim=1))
        return {k: head(h) for k, head in self.heads.items()}


def _mlp(d_in: int, hidden: int, layers: int, dropout: float) -> nn.Sequential:
    mods: list[nn.Module] = []
    for i in range(layers):
        mods += [nn.Linear(d_in if i == 0 else hidden, hidden), nn.ReLU()]
        if dropout:
            mods.append(nn.Dropout(dropout))
    return nn.Sequential(*mods)


def n_params(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters())


# ---------------------------------------------------------------------------

def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def _to(device, *arrs):
    return [torch.from_numpy(a).to(device) for a in arrs]


def loss_and_correct(model: BCNet, cont, cat, tgt) -> tuple[torch.Tensor, dict[str, int]]:
    out = model(cont, cat)
    loss = sum(F.cross_entropy(out[h], tgt[:, i]) for i, h in enumerate(HEADS))
    correct = {h: int((out[h].argmax(1) == tgt[:, i]).sum()) for i, h in enumerate(HEADS)}
    all_ok = torch.stack([out[h].argmax(1) == tgt[:, i] for i, h in enumerate(HEADS)]).all(0)
    correct["all"] = int(all_ok.sum())
    return loss, correct


@torch.no_grad()
def evaluate(model: BCNet, ds: BCDataset, rows: np.ndarray, delays: np.ndarray, device, batch: int) -> dict[str, float]:
    model.eval()
    tot_loss, n = 0.0, 0
    correct = {h: 0 for h in (*HEADS, "all")}
    for s in range(0, len(rows), batch):
        r, d = rows[s:s + batch], delays[s:s + batch]
        cont, cat, tgt = _to(device, *ds.batch(r, d))
        loss, c = loss_and_correct(model, cont, cat, tgt)
        tot_loss += float(loss) * len(r)
        n += len(r)
        for k in correct:
            correct[k] += c[k]
    return {"loss": tot_loss / max(1, n), **{f"acc_{k}": v / max(1, n) for k, v in correct.items()}, "n": n}


def majority_baseline(ds: BCDataset, rows: np.ndarray, delays: np.ndarray, train_rows: np.ndarray, train_delays: np.ndarray) -> dict[str, float]:
    """Val accuracy of always predicting the most common training class, per head."""
    tr = ds.labels[ds.label_row[train_rows, train_delays]]
    va = ds.labels[ds.label_row[rows, delays]]
    return {f"acc_{h}": float((va[:, i] == np.bincount(tr[:, i]).argmax()).mean()) for i, h in enumerate(HEADS)}


def save_checkpoint(path: Path, model: BCNet, extra: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save({
        "format": "super-cpu-bc/1",
        "model_state": model.state_dict(),
        "model_config": asdict(model.mcfg),
        "feature_config": model.fcfg.to_dict(),
        "actions": {
            "main_stick": A.MAIN_STICK, "c_stick": A.C_STICK,
            "button_combos": A.BUTTON_COMBOS, "button_names": A.BUTTON_NAMES,
        },
        **extra,
    }, path)


def load_checkpoint(path: Path | str, device: str = "cpu") -> tuple[BCNet, dict[str, Any]]:
    """Model (in eval mode) and the raw checkpoint dict. Refuses checkpoints
    built for a different action space."""
    ck = torch.load(path, map_location=device, weights_only=True)
    if ck.get("format") != "super-cpu-bc/1":
        raise ValueError(f"{path}: not a super-cpu BC checkpoint")
    acts = ck["actions"]
    if ([tuple(x) for x in acts["main_stick"]] != A.MAIN_STICK or [tuple(x) for x in acts["c_stick"]] != A.C_STICK
            or list(acts["button_combos"]) != A.BUTTON_COMBOS):
        raise ValueError(f"{path}: trained with a different action space than training/actions.py")
    model = BCNet(FeatureConfig(**ck["feature_config"]), ModelConfig(**ck["model_config"])).to(device)
    model.load_state_dict(ck["model_state"])
    model.eval()
    return model, ck


def train(
    files: list[Path] | None = None,
    dataset: BCDataset | None = None,
    fcfg: FeatureConfig = FeatureConfig(),
    mcfg: ModelConfig = ModelConfig(),
    max_delay: int = 4,
    epochs: int = 20,
    batch: int = 512,
    lr: float = 1e-3,
    weight_decay: float = 1e-4,
    val_frac: float = 0.2,
    seed: int = 0,
    device: str = "cpu",
    out_dir: Path | str | None = None,
    expect_version: str | None = EXPECT_VERSION,
    expect_mod_hash: str | None = None,
    max_seconds: float | None = None,
    prev_dropout: float = 0.5,
    log=print,
) -> dict[str, Any]:
    """Train and checkpoint. Returns a summary dict (val metrics, paths, ...)."""
    set_seed(seed)
    if device == "cuda" and not torch.cuda.is_available():
        raise SystemExit("--device cuda but torch.cuda.is_available() is False")
    ds = dataset or build_dataset(files, fcfg, max_delay, expect_version, expect_mod_hash)
    fcfg, max_delay = ds.cfg, ds.max_delay
    tr_m, va_m = split_by_match(len(ds.match_ids), val_frac, seed)
    tr_rows, va_rows = ds.sample_rows(tr_m), ds.sample_rows(va_m)
    if not len(tr_rows):
        raise SystemExit("no training samples (are inputs logged? check input_valid / inputs_present)")
    rng = np.random.default_rng(seed)
    va_delays = ds.pick_delays(va_rows, np.random.default_rng(seed + 1))
    log(f"[bc] {len(ds.match_ids)} matches ({len(tr_m)} train / {len(va_m)} val), "
        f"{len(tr_rows)} train samples, {len(va_rows)} val samples, delay 0..{max_delay}")

    model = BCNet(fcfg, mcfg).to(device)
    log(f"[bc] {mcfg.kind}: {n_params(model):,} params, {fcfg.n_cont} floats + {fcfg.n_cat} categoricals in")
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    run = time.strftime("%Y%m%d-%H%M%S") + f"-{mcfg.kind}-s{seed}"
    out = Path(out_dir) if out_dir else REPO / "checkpoints" / run
    extra = {"max_delay": max_delay, "seed": seed, "prev_dropout": prev_dropout, "train_matches": [ds.match_ids[i] for i in tr_m],
             "val_matches": [ds.match_ids[i] for i in va_m], "expect_version": expect_version}
    best, history = float("inf"), []
    t0 = time.monotonic()
    baseline = majority_baseline(ds, va_rows, va_delays, tr_rows, ds.pick_delays(tr_rows, np.random.default_rng(seed + 2))) if len(va_rows) else {}
    if baseline:
        log("[bc] majority-class val baseline: " + " ".join(f"{k}={v:.3f}" for k, v in baseline.items()))
    for ep in range(1, epochs + 1):
        model.train()
        order = rng.permutation(tr_rows)
        delays = ds.pick_delays(order, rng)  # fresh random delay per sample per epoch
        tl, tn = 0.0, 0
        for s in range(0, len(order), batch):
            cont, cat, tgt = _to(device, *ds.batch(order[s:s + batch], delays[s:s + batch], prev_dropout, rng))
            loss, _ = loss_and_correct(model, cont, cat, tgt)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            tl += loss.item() * len(cont)
            tn += len(cont)
        val = evaluate(model, ds, va_rows, va_delays, device, 4 * batch) if len(va_rows) else {}
        rec = {"epoch": ep, "train_loss": tl / tn, **{f"val_{k}": v for k, v in val.items()}}
        history.append(rec)
        log(f"[bc] epoch {ep:3d} train {tl / tn:.4f}" + (
            f"  val {val['loss']:.4f}  main {val['acc_main']:.3f} cstick {val['acc_cstick']:.3f} "
            f"buttons {val['acc_buttons']:.3f} all {val['acc_all']:.3f}" if val else ""))
        score = val["loss"] if val else tl / tn
        extra_ep = {**extra, "epoch": ep, "metrics": rec, "baseline": baseline}
        if score < best:
            best = score
            save_checkpoint(out / "best.pt", model, extra_ep)
        save_checkpoint(out / "last.pt", model, extra_ep)
        if max_seconds is not None and time.monotonic() - t0 > max_seconds:
            log(f"[bc] stopping after {ep} epochs (--max-seconds)")
            break
    summary = {"out_dir": str(out), "best": str(out / "best.pt"), "history": history, "baseline": baseline,
               "params": n_params(model), "seconds": time.monotonic() - t0}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="behavior cloning on logged matches")
    ap.add_argument("data", nargs="*", default=[str(REPO / "data" / "matches")], help="match files, dirs or globs")
    ap.add_argument("--device", choices=["cuda", "cpu"], default="cuda" if torch.cuda.is_available() else "cpu")
    ap.add_argument("--model", choices=["mlp", "gru"], default="mlp")
    ap.add_argument("--hidden", type=int, default=256)
    ap.add_argument("--layers", type=int, default=2)
    ap.add_argument("--dropout", type=float, default=0.1)
    ap.add_argument("--window", type=int, default=12, help="frames of history, 8..16")
    ap.add_argument("--prev-inputs", type=int, default=4)
    ap.add_argument("--bot-port", type=int, default=1, help="which logged port to clone")
    ap.add_argument("--max-delay", type=int, default=4)
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--batch", type=int, default=512)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--val-frac", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=None, help="checkpoint dir (default checkpoints/<timestamp>-<model>-s<seed>)")
    ap.add_argument("--expect-version", default=EXPECT_VERSION)
    ap.add_argument("--expect-mod-hash", default=None)
    ap.add_argument("--max-seconds", type=float, default=None)
    ap.add_argument("--prev-dropout", type=float, default=0.5,
                    help="chance per sample of hiding the bot's own past inputs (fights copycat feedback)")
    args = ap.parse_args()
    files = find_matches(args.data)
    if not files:
        raise SystemExit(f"no .npz files in {args.data}")
    fcfg = FeatureConfig(window=args.window, prev_inputs=args.prev_inputs, bot_port=args.bot_port)
    mcfg = ModelConfig(kind=args.model, hidden=args.hidden, layers=args.layers, dropout=args.dropout)
    s = train(files, fcfg=fcfg, mcfg=mcfg, max_delay=args.max_delay, epochs=args.epochs, batch=args.batch,
              lr=args.lr, val_frac=args.val_frac, seed=args.seed, device=args.device, out_dir=args.out,
              expect_version=args.expect_version, expect_mod_hash=args.expect_mod_hash,
              max_seconds=args.max_seconds, prev_dropout=args.prev_dropout)
    print(f"[bc] best checkpoint: {s['best']}")


if __name__ == "__main__":
    main()
