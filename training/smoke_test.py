"""End to end check of Phase 2 on synthetic data, no Switch or human logs needed.

    python -m training.smoke_test            # from the repo root
    python -m training.smoke_test --rule scripted --bot-port 2

1. Writes synthetic logged matches (training/synthetic.py) through the real
   MatchWatcher + MatchLogger, labels recorded with record_inputs().
2. Reports the action space round trip on them.
3. Trains the MLP on CPU with random delay 0..4, split by match.
4. Loads the checkpoint through `bridge.policy.POLICIES["bc"]` with
   SCPU_CHECKPOINT, exactly how `--policy bc` will.
5. Plays fresh held-out synthetic matches (different seed) frame by frame
   through a live `History`, with the policy feeding back its own past inputs,
   and compares its output to what the rule does on the frame it's planning
   for. Also times the per-frame act() call.

Exits nonzero if accuracy isn't well above the majority-class baseline.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from bridge.logger import load_match
from bridge.policy import POLICIES, History
from bridge.wire import pack_state, unpack_state

from . import actions as A
from .features import FeatureConfig, find_matches
from .synthetic import make_rule, synthetic_frames, write_synthetic_matches
from .train_bc import ModelConfig, train


def replay_accuracy(policy, rule_name: str, bot_port: int, n_matches: int, n_frames: int, seed: int, delay: int):
    """Per-head accuracy of the live policy vs the rule on unseen matches.
    The policy at frame t is compared to the rule at frame t + delay."""
    import random
    rng = random.Random(seed)
    hits = np.zeros(4)
    n = 0
    times = []
    for m in range(n_matches):
        frames = [unpack_state(pack_state(0, f))[1] for f in synthetic_frames(n_frames, rng, stage_id=0 if m % 2 == 0 else 3)]
        rule = make_rule(rule_name, bot_port)
        rh = History()
        want = [A.encode(rule.act(rh.push(f))) for f in frames]
        policy.reset()
        h = History()
        for i, f in enumerate(frames):
            hist = h.push(f)
            t0 = time.perf_counter()
            policy.act(hist)
            times.append(time.perf_counter() - t0)
            if i + delay >= len(frames):
                continue
            got, w = policy.last_classes, want[i + delay]
            row = [got[0] == w.main, got[1] == w.cstick, got[2] == w.buttons]
            hits += row + [all(row)]
            n += 1
    return hits / n, n, np.array(times)


def majority(rule_name: str, bot_port: int, n_matches: int, n_frames: int, seed: int) -> np.ndarray:
    import random
    rng = random.Random(seed)
    labels = []
    for m in range(n_matches):
        rule = make_rule(rule_name, bot_port)
        h = History()
        for f in synthetic_frames(n_frames, rng, 0):
            c = A.encode(rule.act(h.push(unpack_state(pack_state(0, f))[1])))
            labels.append((c.main, c.cstick, c.buttons))
    lab = np.array(labels)
    accs = [(lab[:, i] == np.bincount(lab[:, i]).argmax()).mean() for i in range(3)]
    return np.array(accs)


def run(rule: str = "rule", bot_port: int = 1, n_train: int = 12, n_frames: int = 1200, epochs: int = 20,
        seed: int = 0, out: Path | None = None, log=print) -> dict:
    tmp = tempfile.TemporaryDirectory(prefix="scpu-smoke-")
    root = Path(out) if out else Path(tmp.name)
    t_all = time.monotonic()
    paths = write_synthetic_matches(root / "matches", n_matches=n_train, n_frames=n_frames, rule=rule,
                                    bot_port=bot_port, seed=seed)
    files = find_matches([root / "matches"])
    assert len(paths) == len(files) == n_train, (paths, files)
    rep = A.roundtrip_report([load_match(p, expect_version="13.0.4") for p in files], bot_port)
    log("[smoke] action space round trip on synthetic logs:\n" + rep.text())

    t0 = time.monotonic()
    summary = train(files, fcfg=FeatureConfig(bot_port=bot_port), mcfg=ModelConfig(), max_delay=4,
                    epochs=epochs, seed=seed, device="cpu", out_dir=root / "ckpt", log=log)
    train_s = time.monotonic() - t0
    last = summary["history"][-1]
    log(f"[smoke] trained in {train_s:.1f}s, {summary['params']:,} params")

    os.environ["SCPU_CHECKPOINT"] = summary["best"]
    os.environ["SCPU_TEMPERATURE"] = "0"  # accuracy check: greedy, like the val numbers
    results = {}
    base = majority(rule, bot_port, 4, n_frames, seed + 1000)
    for delay in (0, 2, 4):
        os.environ["SCPU_DELAY"] = str(delay)
        policy = POLICIES["bc"]()
        acc, n, times = replay_accuracy(policy, rule, bot_port, 4, n_frames, seed + 1000, delay)
        results[delay] = acc
        log(f"[smoke] held-out live replay, delay {delay}: main {acc[0]:.3f} cstick {acc[1]:.3f} "
            f"buttons {acc[2]:.3f} all {acc[3]:.3f} over {n} frames")
    log(f"[smoke] majority-class baseline on held-out: main {base[0]:.3f} cstick {base[1]:.3f} buttons {base[2]:.3f}")
    log(f"[smoke] act() per frame on CPU (features + forward + decode): median {np.median(times) * 1e3:.3f} ms, "
        f"p99 {np.percentile(times, 99) * 1e3:.3f} ms")
    log(f"[smoke] total {time.monotonic() - t_all:.1f}s")
    tmp.cleanup()
    return {"val": last, "train_s": train_s, "replay": results, "baseline": base, "act_ms_median": float(np.median(times) * 1e3),
            "params": summary["params"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", choices=["rule", "scripted", "walk"], default="rule")
    ap.add_argument("--bot-port", type=int, default=1)
    ap.add_argument("--matches", type=int, default=12)
    ap.add_argument("--frames", type=int, default=1200)
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=None, help="keep the synthetic data and checkpoint here")
    args = ap.parse_args()
    r = run(args.rule, args.bot_port, args.matches, args.frames, args.epochs, args.seed,
            Path(args.out) if args.out else None)
    acc0 = r["replay"][0]
    # "well above chance": per head, at least halfway from the majority baseline to 100%
    ok = all(acc0[i] >= r["baseline"][i] + 0.5 * (1 - r["baseline"][i]) - 1e-9 for i in range(3))
    print("[smoke] PASS" if ok else "[smoke] FAIL: live replay accuracy not well above baseline")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
