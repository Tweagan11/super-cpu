"""Inverse dynamics model (SSB-26 / KAN-34), like OpenAI's VPT.

    python -m training.idm train data/matches --out checkpoints/idm1
    python -m training.idm eval  checkpoints/idm1/best.pt data/matches
    python -m training.idm label checkpoints/idm1/best.pt replay.npz --port 2

A behavior cloning policy sees state up to frame t and guesses the input. An
IDM sees state AROUND frame t (past and future) and guesses the input that was
pressed on t. That is a much easier problem (the future shows what the stick
and buttons did), so a modest model trained on logs with real inputs can label
logs without them: replays streamed through the plugin, or ports with
input_valid = 0. Those labeled rows become behavior cloning data.

## Design

- Features are `features.window_features`, unchanged. A sample is (row, port):
  "me" is that port (the same perspective switch as bot_port), the window is
  `--window` game frames ending at frame t + `--future` (default 16 frames,
  8 future, so t-7 .. t+8). t sits at a fixed slot, so the model knows where
  it is. No prev inputs (an IDM must work where there are none), delay input
  is 0. Needs frame t + future to be in the log, so the last few frames of a
  match and rows next to a dropped frame at t+future can't be labeled.
- Model: `train_bc.BCNet` (mlp), same three heads, same action classes.
- Labels: recorded inputs only (input_valid = 1). Both ports are used.
  Rows inferred by an earlier `label` run are never training labels.
- Split by match, seeded, like train_bc.

## Labeled files

`label` never touches the input. It writes `<stem>.inferred.npz` (+ .json)
next to it, same arrays plus a bool array `inferred` (n, 2). On marked rows the
input columns hold the prediction but `input_valid` stays 0, so anything that
doesn't know about IDMs still treats them as unknown. The header gets:

    "derived_from": "<raw match_id>",
    "inferred_inputs": {"2": {"model": ..., "rows": N, "unlabeled_rows": M, ...}}

`features.build_dataset` / `train_bc --include-inferred` turn them into labels;
default is off. When a raw log and its labeled copy are both loaded only one is
used, so a directory with both isn't double counted.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn.functional as F

from bridge.logger import INPUT_FIELDS, LoadedMatch, load_match

from . import actions as A
from .features import (
    EXPECT_VERSION,
    FeatureConfig,
    find_matches,
    load_all,
    split_by_match,
    valid_input_mask,
    window_features,
)
from .train_bc import HEADS, REPO, BCNet, ModelConfig, n_params, save_checkpoint, set_seed

FORMAT = "super-cpu-idm/1"
HEAD_SIZES = (A.N_MAIN, A.N_CSTICK, A.N_BUTTONS)
INFERRED_SUFFIX = ".inferred"


# ---------------------------------------------------------------------------
# Data

class IDMData:
    """Concatenated matches. Samples are (row, port) pairs; `end_row[i]` is the
    row of frame t_i + future (-1 when it isn't logged)."""

    def __init__(self, matches: list[LoadedMatch], cfg: FeatureConfig, future: int,
                 labels_from: str = "recorded") -> None:
        if not 0 <= future < cfg.window:
            raise ValueError(f"future must be in 0..{cfg.window - 1}")
        self.cfg, self.future = cfg, future
        self.matches = matches
        self.match_ids = [m.header.get("match_id", str(i)) for i, m in enumerate(matches)]
        keys, starts, sids, pls, midx, labels = [], [], [], [], [], []
        off = 0
        for i, m in enumerate(matches):
            n = len(m.frames)
            fr = m.frames["frame"].astype(np.int64)
            if n > 1 and (np.diff(fr) <= 0).any():
                raise ValueError(f"match {self.match_ids[i]}: frame numbers not increasing")
            keys.append((np.int64(i) << 32) | fr)
            starts.append(np.full(n, off, np.int64))
            sids.append(m.frames["stage_id"])
            pls.append(m.players)
            midx.append(np.full(n, i, np.int64))
            lab = np.full((n, 2, 3), -1, np.int64)
            for port in (1, 2):
                p = m.port(port)
                cls = np.stack(A.encode_arrays(p["stick_x"], p["stick_y"], p["cstick_x"], p["cstick_y"], p["buttons"]), axis=1)
                ok = valid_input_mask(m, port, include_inferred=False)  # IDM never learns from its own output
                lab[ok, port - 1] = cls[ok]
            labels.append(lab)
            off += n
        self.keys = np.concatenate(keys)
        self.starts = np.concatenate(starts)
        self.stage_ids = np.concatenate(sids)
        self.players = np.concatenate(pls)
        self.match_index = np.concatenate(midx)
        self.labels = np.concatenate(labels)
        N = len(self.keys)
        want = self.keys + future
        pos = np.minimum(np.searchsorted(self.keys, want), N - 1)
        self.end_row = np.where(self.keys[pos] == want, pos, -1)

    def samples(self, matches: list[int] | None = None, port: int | None = None,
                labeled: bool = True) -> tuple[np.ndarray, np.ndarray]:
        """(rows, ports 1-based) with a usable window and, if `labeled`, a recorded label."""
        ok = self.end_row >= 0
        if matches is not None:
            ok &= np.isin(self.match_index, np.asarray(matches, np.int64))
        rows, ports = [], []
        for p in (1, 2) if port is None else (port,):
            sel = ok & ((self.labels[:, p - 1, 0] >= 0) if labeled else True)
            r = np.flatnonzero(sel)
            rows.append(r)
            ports.append(np.full(len(r), p, np.int64))
        return np.concatenate(rows), np.concatenate(ports)

    def features(self, rows: np.ndarray, ports: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        cont = np.empty((len(rows), self.cfg.n_cont), np.float32)
        cat = np.empty((len(rows), self.cfg.n_cat), np.int64)
        for p in (1, 2):
            sel = np.flatnonzero(ports == p)
            if not len(sel):
                continue
            n = len(sel)
            c, k = window_features(
                self.keys, self.starts, self.stage_ids, self.players,
                self.end_row[rows[sel]], np.zeros(n, np.int64),
                np.zeros((n, self.cfg.prev_inputs, 3), np.int64), replace(self.cfg, bot_port=p),
            )
            cont[sel], cat[sel] = c, k
        return cont, cat

    def targets(self, rows: np.ndarray, ports: np.ndarray) -> np.ndarray:
        return self.labels[rows, ports - 1]


def build_idm_data(matches, window: int = 16, future: int = 8, expect_version: str | None = EXPECT_VERSION,
                   expect_mod_hash: str | None = None) -> IDMData:
    loaded = load_all(matches, expect_version, expect_mod_hash, include_inferred=False)
    return IDMData(loaded, FeatureConfig(window=window, prev_inputs=0), future)


def _t(device, *arrs):
    return [torch.from_numpy(a).to(device) for a in arrs]


# ---------------------------------------------------------------------------
# Checkpoints

def save_idm(path: Path, model: BCNet, future: int, extra: dict[str, Any]) -> None:
    save_checkpoint(path, model, {"format": FORMAT, "future": future, **extra})


def load_idm(path: Path | str, device: str = "cpu") -> tuple[BCNet, dict[str, Any]]:
    ck = torch.load(path, map_location=device, weights_only=True)
    if ck.get("format") != FORMAT:
        raise ValueError(f"{path}: not a super-cpu IDM checkpoint")
    acts = ck["actions"]
    if ([tuple(x) for x in acts["main_stick"]] != A.MAIN_STICK or [tuple(x) for x in acts["c_stick"]] != A.C_STICK
            or list(acts["button_combos"]) != A.BUTTON_COMBOS):
        raise ValueError(f"{path}: trained with a different action space than training/actions.py")
    model = BCNet(FeatureConfig(**ck["feature_config"]), ModelConfig(**ck["model_config"])).to(device)
    model.load_state_dict(ck["model_state"])
    model.eval()
    return model, ck


# ---------------------------------------------------------------------------
# Train

@torch.no_grad()
def predict(model: BCNet, ds: IDMData, rows: np.ndarray, ports: np.ndarray, device="cpu", batch: int = 2048,
            ) -> tuple[np.ndarray, np.ndarray]:
    """(classes (B, 3) int64, confidence (B,) float: product of the three head maxima)."""
    model.eval()
    cls, conf = [], []
    for s in range(0, len(rows), batch):
        cont, cat = _t(device, *ds.features(rows[s:s + batch], ports[s:s + batch]))
        out = model(cont, cat)
        pr = [F.softmax(out[h], dim=1) for h in HEADS]
        cls.append(torch.stack([p.argmax(1) for p in pr], 1).cpu().numpy())
        conf.append(torch.stack([p.max(1).values for p in pr], 1).prod(1).cpu().numpy())
    if not cls:
        return np.zeros((0, 3), np.int64), np.zeros(0, np.float32)
    return np.concatenate(cls), np.concatenate(conf)


def majority(train_tgt: np.ndarray) -> tuple[np.ndarray, tuple]:
    """Per-head most common class, and the most common exact (main, cstick, buttons)."""
    heads = np.array([np.bincount(train_tgt[:, i], minlength=HEAD_SIZES[i]).argmax() for i in range(3)])
    combo = np.unique(train_tgt, axis=0, return_counts=True)
    return heads, tuple(combo[0][combo[1].argmax()])


def train(
    files, window: int = 16, future: int = 8, mcfg: ModelConfig = ModelConfig(), epochs: int = 20,
    batch: int = 512, lr: float = 1e-3, weight_decay: float = 1e-4, val_frac: float = 0.2, seed: int = 0,
    device: str = "cpu", out_dir: Path | str | None = None, expect_version: str | None = EXPECT_VERSION,
    expect_mod_hash: str | None = None, max_seconds: float | None = None, log=print,
) -> dict[str, Any]:
    set_seed(seed)
    if device == "cuda" and not torch.cuda.is_available():
        raise SystemExit("--device cuda but torch.cuda.is_available() is False")
    ds = build_idm_data(files, window, future, expect_version, expect_mod_hash)
    tr_m, va_m = split_by_match(len(ds.match_ids), val_frac, seed)
    tr_rows, tr_ports = ds.samples(tr_m)
    va_rows, va_ports = ds.samples(va_m)
    if not len(tr_rows):
        raise SystemExit("no labeled samples (need rows with input_valid = 1 on some port)")
    log(f"[idm] {len(ds.match_ids)} matches ({len(tr_m)} train / {len(va_m)} val), window {window} "
        f"(t-{window - 1 - future}..t+{future}), {len(tr_rows)} train / {len(va_rows)} val samples "
        f"(ports: {sorted(set(tr_ports.tolist()))})")
    model = BCNet(ds.cfg, mcfg).to(device)
    log(f"[idm] {n_params(model):,} params")
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    run = time.strftime("%Y%m%d-%H%M%S") + f"-idm-s{seed}"
    out = Path(out_dir) if out_dir else REPO / "checkpoints" / run
    base_heads, base_combo = majority(ds.targets(tr_rows, tr_ports))
    baseline = {}
    if len(va_rows):
        vt = ds.targets(va_rows, va_ports)
        baseline = {f"acc_{h}": float((vt[:, i] == base_heads[i]).mean()) for i, h in enumerate(HEADS)}
        baseline["acc_all"] = float((vt == np.array(base_combo)).all(1).mean())
        log("[idm] majority-class val baseline: " + " ".join(f"{k}={v:.3f}" for k, v in baseline.items()))
    extra = {"seed": seed, "train_matches": [ds.match_ids[i] for i in tr_m], "val_matches": [ds.match_ids[i] for i in va_m],
             "expect_version": expect_version, "baseline": baseline}
    rng = np.random.default_rng(seed)
    best, history, t0 = float("inf"), [], time.monotonic()
    for ep in range(1, epochs + 1):
        model.train()
        order = rng.permutation(len(tr_rows))
        tl = 0.0
        for s in range(0, len(order), batch):
            i = order[s:s + batch]
            cont, cat = _t(device, *ds.features(tr_rows[i], tr_ports[i]))
            tgt = torch.from_numpy(ds.targets(tr_rows[i], tr_ports[i])).to(device)
            outp = model(cont, cat)
            loss = sum(F.cross_entropy(outp[h], tgt[:, k]) for k, h in enumerate(HEADS))
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            tl += loss.item() * len(i)
        rec: dict[str, Any] = {"epoch": ep, "train_loss": tl / len(order)}
        msg = f"[idm] epoch {ep:3d} train {rec['train_loss']:.4f}"
        if len(va_rows):
            rep = evaluate(model, ds, va_rows, va_ports, device, with_confusion=False)
            rec.update({f"val_{k}": v for k, v in rep["acc"].items()}, val_loss=rep["loss"])
            msg += f"  val {rep['loss']:.4f}  main {rep['acc']['main']:.3f} cstick {rep['acc']['cstick']:.3f} " \
                   f"buttons {rep['acc']['buttons']:.3f} exact {rep['acc']['all']:.3f}"
        log(msg)
        history.append(rec)
        score = rec.get("val_loss", rec["train_loss"])
        meta = {**extra, "epoch": ep, "metrics": rec}
        if score < best:
            best = score
            save_idm(out / "best.pt", model, future, meta)
        save_idm(out / "last.pt", model, future, meta)
        if max_seconds is not None and time.monotonic() - t0 > max_seconds:
            log(f"[idm] stopping after {ep} epochs (--max-seconds)")
            break
    summary = {"out_dir": str(out), "best": str(out / "best.pt"), "history": history, "baseline": baseline,
               "params": n_params(model), "seconds": time.monotonic() - t0}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


# ---------------------------------------------------------------------------
# Eval

def class_names() -> list[list[str]]:
    btn = list(A.BUTTON_NAMES)
    return [[f"{x},{y}" for x, y in A.MAIN_STICK], [f"{x},{y}" for x, y in A.C_STICK], btn]


@torch.no_grad()
def evaluate(model: BCNet, ds: IDMData, rows: np.ndarray, ports: np.ndarray, device="cpu",
             batch: int = 2048, with_confusion: bool = True) -> dict[str, Any]:
    model.eval()
    preds, losses = [], 0.0
    for s in range(0, len(rows), batch):
        r, p = rows[s:s + batch], ports[s:s + batch]
        cont, cat = _t(device, *ds.features(r, p))
        tgt = torch.from_numpy(ds.targets(r, p)).to(device)
        out = model(cont, cat)
        losses += float(sum(F.cross_entropy(out[h], tgt[:, k], reduction="sum") for k, h in enumerate(HEADS)))
        preds.append(torch.stack([out[h].argmax(1) for h in HEADS], 1).cpu().numpy())
    pred = np.concatenate(preds) if preds else np.zeros((0, 3), np.int64)
    tgt = ds.targets(rows, ports)
    n = max(1, len(rows))
    acc = {h: float((pred[:, i] == tgt[:, i]).mean()) if len(rows) else 0.0 for i, h in enumerate(HEADS)}
    acc["all"] = float((pred == tgt).all(1).mean()) if len(rows) else 0.0
    res: dict[str, Any] = {"n": len(rows), "loss": losses / n, "acc": acc, "pred": pred, "tgt": tgt}
    if with_confusion:
        res["confusion"] = [np.bincount(tgt[:, i] * HEAD_SIZES[i] + pred[:, i], minlength=HEAD_SIZES[i] ** 2)
                            .reshape(HEAD_SIZES[i], HEAD_SIZES[i]) for i in range(3)]
    return res


def report(res: dict[str, Any], train_tgt: np.ndarray, top: int = 4) -> str:
    """Text report: per-head accuracy vs majority baseline, exact action, confusion highlights, hard buttons."""
    names = class_names()
    heads, combo = majority(train_tgt)
    tgt, acc = res["tgt"], res["acc"]
    lines = [f"samples: {res['n']}", "head      acc    majority-baseline"]
    for i, h in enumerate(HEADS):
        lines.append(f"{h:<9} {acc[h]:.3f}  {(tgt[:, i] == heads[i]).mean():.3f}  (always '{names[i][heads[i]]}')")
    lines.append(f"exact     {acc['all']:.3f}  {(tgt == np.array(combo)).all(1).mean():.3f}  (always {tuple(int(c) for c in combo)})")
    for i, h in enumerate(HEADS):
        cm = res["confusion"][i]
        off = [(int(cm[a, b]), a, b) for a in range(cm.shape[0]) for b in range(cm.shape[1]) if a != b and cm[a, b]]
        off.sort(reverse=True)
        lines.append(f"\n{h}: most common confusions (true -> predicted, count, share of that true class)")
        for c, a, b in off[:top]:
            lines.append(f"  {names[i][a]:>12} -> {names[i][b]:<12} {c:6d}  {c / cm[a].sum():.0%}")
    cm = res["confusion"][2]
    rows = []
    for k in range(A.N_BUTTONS):
        sup, hit = int(cm[k].sum()), int(cm[k, k])
        col = int(cm[:, k].sum())
        if sup:
            rows.append((hit / sup, k, sup, hit / col if col else 0.0))
    lines.append("\nbutton classes by recall (hardest first; HARD = recall < 50%)")
    for rec, k, sup, prec in sorted(rows):
        lines.append(f"  {names[2][k]:<12} recall {rec:.2f} precision {prec:.2f} support {sup:6d}{'  HARD' if rec < 0.5 else ''}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Label

def default_out_path(src: Path) -> Path:
    stem = src.stem
    while stem.endswith(INFERRED_SUFFIX):
        stem = stem[: -len(INFERRED_SUFFIX)]
    return src.with_name(stem + INFERRED_SUFFIX + ".npz")


def label_match(
    model: BCNet, ck: dict[str, Any], src: Path | str, port: int, out: Path | str | None = None,
    device: str = "cpu", min_conf: float = 0.0, model_name: str | None = None,
    expect_version: str | None = EXPECT_VERSION, log=print,
) -> dict[str, Any]:
    """Predict inputs for the rows of `port` that have none and write a NEW
    file. Returns the stats written into the header."""
    src = Path(src)
    out = Path(out) if out else default_out_path(src)
    if out.resolve() == src.resolve():
        raise SystemExit(f"refusing to overwrite {src}; pass a different --out")
    if not out.name.endswith(INFERRED_SUFFIX + ".npz"):
        raise SystemExit(f"--out must end with {INFERRED_SUFFIX}.npz so it can't be mistaken for a raw log")
    m = load_match(src, expect_version)
    fcfg = FeatureConfig(**ck["feature_config"])
    ds = IDMData([m], fcfg, int(ck["future"]))
    prior = m.inferred if m.inferred is not None else np.zeros((len(m.frames), 2), bool)
    need = (m.port(port)["input_valid"] == 0) & ~prior[:, port - 1]
    cand = np.flatnonzero(need & (ds.end_row >= 0))
    cls, conf = predict(model, ds, cand, np.full(len(cand), port, np.int64), device)
    keep = conf >= min_conf
    rows, cls = cand[keep], cls[keep]
    players, inferred = m.players.copy(), prior.copy()
    sx, sy, cx, cy, btn = A.decode_arrays(cls[:, 0], cls[:, 1], cls[:, 2])
    p = players[:, port - 1]
    for name, v in zip(INPUT_FIELDS, (sx, sy, cx, cy, btn)):
        p[name][rows] = v
    inferred[rows, port - 1] = True
    stats = {
        "model": model_name or str(ck.get("model_name") or "idm"),
        "rows": int(len(rows)),
        "unlabeled_rows": int(need.sum() - len(rows)),
        "future": int(ck["future"]),
        "window": fcfg.window,
        "min_conf": min_conf,
        "mean_conf": round(float(conf[keep].mean()), 4) if keep.any() else None,
    }
    header = dict(m.header)
    header["derived_from"] = m.header.get("derived_from") or m.header.get("match_id")
    header["match_id"] = str(m.header.get("match_id", src.stem)).removesuffix("-idm") + "-idm"
    header["inferred_inputs"] = {**m.header.get("inferred_inputs", {}), str(port): stats}
    text = json.dumps(header, indent=2)
    tmp = out.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, header=np.array(text), frames=m.frames, players=players, inferred=inferred)
    os.replace(tmp, out)
    out.with_suffix(".json").write_text(text + "\n")
    log(f"[idm] {out.name}: port {port}: inferred {stats['rows']} rows, {stats['unlabeled_rows']} left unlabeled")
    return {**stats, "path": str(out)}


# ---------------------------------------------------------------------------
# CLI

def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m training.idm", description="inverse dynamics model")
    sub = ap.add_subparsers(dest="cmd", required=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"

    t = sub.add_parser("train", help="train on logs with recorded inputs (any port with input_valid = 1)")
    t.add_argument("data", nargs="*", default=[str(REPO / "data" / "matches")])
    t.add_argument("--device", choices=["cuda", "cpu"], default=dev)
    t.add_argument("--window", type=int, default=16, help="frames in the window, 8..16")
    t.add_argument("--future", type=int, default=8, help="how many of them come after t")
    t.add_argument("--hidden", type=int, default=256)
    t.add_argument("--layers", type=int, default=2)
    t.add_argument("--dropout", type=float, default=0.1)
    t.add_argument("--epochs", type=int, default=20)
    t.add_argument("--batch", type=int, default=512)
    t.add_argument("--lr", type=float, default=1e-3)
    t.add_argument("--val-frac", type=float, default=0.2)
    t.add_argument("--seed", type=int, default=0)
    t.add_argument("--out", default=None)
    t.add_argument("--expect-version", default=EXPECT_VERSION)
    t.add_argument("--expect-mod-hash", default=None)
    t.add_argument("--max-seconds", type=float, default=None)

    e = sub.add_parser("eval", help="accuracy vs majority baseline, confusions, hard buttons")
    e.add_argument("checkpoint")
    e.add_argument("data", nargs="*", default=[str(REPO / "data" / "matches")])
    e.add_argument("--split", choices=["val", "train", "all"], default="val", help="matches the checkpoint held out (default) or trained on")
    e.add_argument("--device", choices=["cuda", "cpu"], default=dev)
    e.add_argument("--expect-version", default=EXPECT_VERSION)

    l = sub.add_parser("label", help="write <stem>.inferred.npz with predicted inputs for rows with none")
    l.add_argument("checkpoint")
    l.add_argument("data", nargs="+", help="match files (raw logs are never modified)")
    l.add_argument("--port", type=int, choices=[1, 2], required=True)
    l.add_argument("--out", default=None, help="output path (one input only); default <stem>.inferred.npz")
    l.add_argument("--min-conf", type=float, default=0.0, help="leave rows below this confidence unlabeled")
    l.add_argument("--device", choices=["cuda", "cpu"], default="cpu")
    l.add_argument("--expect-version", default=EXPECT_VERSION)

    a = ap.parse_args()
    if a.cmd == "train":
        mcfg = ModelConfig(hidden=a.hidden, layers=a.layers, dropout=a.dropout)
        files = find_matches(a.data)
        if not files:
            raise SystemExit(f"no .npz files in {a.data}")
        s = train(files, a.window, a.future, mcfg, a.epochs, a.batch, a.lr, val_frac=a.val_frac, seed=a.seed,
                  device=a.device, out_dir=a.out, expect_version=a.expect_version,
                  expect_mod_hash=a.expect_mod_hash, max_seconds=a.max_seconds)
        print(f"[idm] best checkpoint: {s['best']}")
    elif a.cmd == "eval":
        model, ck = load_idm(a.checkpoint, a.device)
        files = find_matches(a.data)
        loaded = load_all(files, a.expect_version, None, include_inferred=False)
        ds = IDMData(loaded, FeatureConfig(**ck["feature_config"]), int(ck["future"]))
        pick = {"val": ck["val_matches"], "train": ck["train_matches"]}.get(a.split)
        idx = [i for i, mid in enumerate(ds.match_ids) if pick is None or mid in pick]
        if not idx:
            raise SystemExit(f"none of these files are in the checkpoint's {a.split} split; try --split all")
        rows, ports = ds.samples(idx)
        tr_rows, tr_ports = ds.samples([i for i, mid in enumerate(ds.match_ids) if mid in ck["train_matches"]] or idx)
        res = evaluate(model.to(a.device), ds, rows, ports, a.device)
        print(report(res, ds.targets(tr_rows, tr_ports)))
    else:
        if a.out and len(a.data) != 1:
            raise SystemExit("--out only works with one input")
        model, ck = load_idm(a.checkpoint, a.device)
        for f in find_matches(a.data):
            if f.name.endswith(INFERRED_SUFFIX + ".npz") and not a.out:
                continue  # already labeled copies
            label_match(model, ck, f, a.port, a.out, a.device, a.min_conf, Path(a.checkpoint).parent.name,
                        a.expect_version)


if __name__ == "__main__":
    main()
