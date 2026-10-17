"""Trainer + bc policy tests. Skipped when torch isn't installed."""
import os
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

import training  # noqa: F401

try:
    import torch
except ImportError:  # pragma: no cover
    torch = None


@unittest.skipIf(torch is None, "torch not installed (pip install -e 'bridge[train]')")
class TrainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from training.synthetic import write_synthetic_matches
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.paths = write_synthetic_matches(cls.dir / "m", n_matches=6, n_frames=600, seed=11)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _train(self, out, **kw):
        from training.train_bc import ModelConfig, train
        kw.setdefault("mcfg", ModelConfig(hidden=64))
        return train(self.paths, epochs=kw.pop("epochs", 3), out_dir=self.dir / out, log=lambda s: None, **kw)

    def test_train_checkpoint_and_policy(self):
        from bridge.policy import POLICIES, History
        from bridge.protocol import Inputs
        from training.bc_policy import BCPolicy
        from training.synthetic import synthetic_frames
        import random

        s = self._train("a", epochs=10)
        self.assertTrue(Path(s["best"]).exists())
        self.assertTrue((self.dir / "a" / "last.pt").exists())
        best = max(h["val_acc_main"] for h in s["history"])
        self.assertGreater(best, s["baseline"]["acc_main"])

        os.environ["SCPU_CHECKPOINT"] = s["best"]
        os.environ.pop("SCPU_DELAY", None)
        try:
            p = POLICIES["bc"]()
        finally:
            del os.environ["SCPU_CHECKPOINT"]
        self.assertIsInstance(p, BCPolicy)
        self.assertEqual(p.delay, 4)  # defaults to the checkpoint's max delay
        h = History()
        for f in synthetic_frames(40, random.Random(0), 0):
            out = p.act(h.push(f))
            self.assertIsInstance(out, Inputs)
        self.assertEqual(len(p.prev), p.cfg.prev_inputs)
        p.configure(delay=3)
        self.assertEqual(p.delay, 3, "bridge --delay wins without SCPU_DELAY")

        # Sampling (default) vs greedy on the same frames: sampling has to pick
        # more than one class per head over time, greedy is deterministic.
        frames = synthetic_frames(120, random.Random(1), 0)
        def run(temp):
            os.environ["SCPU_CHECKPOINT"] = s["best"]
            os.environ["SCPU_TEMPERATURE"] = temp
            os.environ["SCPU_SEED"] = "3"
            try:
                q = POLICIES["bc"]()
            finally:
                for k in ("SCPU_CHECKPOINT", "SCPU_TEMPERATURE", "SCPU_SEED"):
                    del os.environ[k]
            hh = History()
            return [q.predict(list(hh.push(f))) for f in frames]
        greedy, sampled = run("0"), run("1.0")
        self.assertEqual(greedy, run("0"))
        self.assertNotEqual(greedy, sampled)

    def test_hold_changes_less_but_still_acts(self):
        """Hold (default 4) vs per-frame sampling: fewer changes frame to
        frame, but it still changes (a stuck bot is the hardware bug)."""
        from bridge.policy import POLICIES, History
        from training.synthetic import synthetic_frames
        import random
        s = self._train("st", epochs=2)
        frames = synthetic_frames(200, random.Random(2), 0)

        def changes(hold):
            os.environ.update(SCPU_CHECKPOINT=s["best"], SCPU_SEED="5", SCPU_HOLD=hold)
            try:
                q = POLICIES["bc"]()
            finally:
                for k in ("SCPU_CHECKPOINT", "SCPU_SEED", "SCPU_HOLD"):
                    del os.environ[k]
            hh = History()
            outs = []
            for f in frames:
                q.act(hh.push(f))
                outs.append(q.last_classes)
            return sum(a != b for a, b in zip(outs, outs[1:]))

        held, every = changes("4"), changes("1")
        self.assertLess(held, every)
        self.assertGreater(held, 200 // 4 // 4, "still picks new inputs")

    def test_bc_needs_checkpoint(self):
        from training.bc_policy import BCPolicy
        os.environ.pop("SCPU_CHECKPOINT", None)
        with self.assertRaises(ValueError):
            BCPolicy()

    def test_deterministic(self):
        a = self._train("d1", epochs=2)
        b = self._train("d2", epochs=2)
        self.assertEqual(a["history"], b["history"])

    def test_gru_variant(self):
        from training.train_bc import ModelConfig, load_checkpoint
        s = self._train("g", epochs=1, mcfg=ModelConfig(kind="gru", hidden=32))
        model, ck = load_checkpoint(s["best"])
        self.assertEqual(model.mcfg.kind, "gru")

    def test_rejects_other_action_space(self):
        from training import actions as A
        from training.train_bc import load_checkpoint
        s = self._train("x", epochs=1)
        ck = torch.load(s["best"], weights_only=True)
        ck["actions"]["button_combos"] = A.BUTTON_COMBOS[:-1]
        bad = self.dir / "bad.pt"
        torch.save(ck, bad)
        with self.assertRaises(ValueError):
            load_checkpoint(bad)

    def test_inference_fast_on_cpu(self):
        """Default-size MLP: features + forward + decode per frame, CPU, 1 thread."""
        import random
        from bridge.policy import History
        from training.bc_policy import BCPolicy
        from training.synthetic import synthetic_frames
        from training.train_bc import ModelConfig, BCNet, save_checkpoint
        from training.features import FeatureConfig
        path = self.dir / "speed.pt"
        save_checkpoint(path, BCNet(FeatureConfig(), ModelConfig()).eval(), {"max_delay": 4})
        p = BCPolicy(path)
        h = History()
        frames = synthetic_frames(400, random.Random(0), 0)
        times = []
        for f in frames:
            hist = h.push(f)
            t = time.perf_counter()
            p.act(hist)
            times.append(time.perf_counter() - t)
        med = float(np.median(times[50:]))
        print(f"\n[bc] act() median {med * 1e3:.3f} ms on CPU")
        self.assertLess(med, 0.004)  # generous for slow CI boxes; ~0.5 ms typical


if __name__ == "__main__":
    unittest.main()
