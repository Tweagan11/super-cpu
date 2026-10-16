"""IDM tests: train on synthetic matches, label a match with inputs removed,
and the include/exclude flag. Skipped when torch isn't installed."""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

import training  # noqa: F401

try:
    import torch
except ImportError:  # pragma: no cover
    torch = None


@unittest.skipIf(torch is None, "torch not installed (pip install -e 'bridge[train]')")
class IDMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from training.idm import train
        from training.train_bc import ModelConfig
        from training.synthetic import write_synthetic_matches
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        # bot on port 1 in some matches, port 2 in others: both ports are usable
        cls.paths = (write_synthetic_matches(cls.dir / "m", n_matches=4, n_frames=500, seed=21)
                     + write_synthetic_matches(cls.dir / "m", n_matches=4, n_frames=500, bot_port=2, seed=22))
        cls.summary = train(cls.paths, mcfg=ModelConfig(hidden=128), epochs=25, out_dir=cls.dir / "ck",
                            log=lambda s: None)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_beats_baseline_on_val(self):
        s = self.summary
        last = {k: max(h[k] for h in s["history"][-5:]) for k in s["history"][-1] if k.startswith("val_")}
        self.assertGreater(last["val_main"], s["baseline"]["acc_main"] + 0.3)
        self.assertGreater(last["val_buttons"], s["baseline"]["acc_buttons"] + 0.03)
        self.assertGreater(last["val_all"], s["baseline"]["acc_all"] + 0.3)
        # the c-stick is almost always centered in the synthetic rule, so the baseline is ~99%
        self.assertGreater(last["val_cstick"], s["baseline"]["acc_cstick"] - 0.02)
        self.assertGreater(last["val_all"], 0.6)

    def test_eval_report(self):
        from training.idm import build_idm_data, evaluate, load_idm, report
        model, ck = load_idm(self.summary["best"])
        ds = build_idm_data(self.paths)
        idx = [i for i, m in enumerate(ds.match_ids) if m in ck["val_matches"]]
        rows, ports = ds.samples(idx)
        txt = report(evaluate(model, ds, rows, ports), ds.targets(*ds.samples()))
        self.assertIn("majority-baseline", txt)
        self.assertIn("button classes by recall", txt)

    def _strip(self, src: Path, port: int) -> Path:
        """Copy of a raw log with one port's inputs removed (what a replay looks like)."""
        from bridge.logger import load_match
        m = load_match(src)
        pl = m.players.copy()
        for k in ("stick_x", "stick_y", "cstick_x", "cstick_y", "buttons", "input_valid"):
            pl[k][:, port - 1] = 0
        out = self.dir / "replay" / (src.stem + "-stripped.npz")
        out.parent.mkdir(exist_ok=True)
        header = {**m.header, "match_id": m.header["match_id"] + "-stripped"}
        np.savez_compressed(out, header=np.array(json.dumps(header)), frames=m.frames, players=pl)
        return out

    def test_label_marks_and_excludes_by_default(self):
        from bridge.logger import load_match
        from training import actions as A
        from training.features import FeatureConfig, build_dataset
        from training.idm import label_match, load_idm
        model, ck = load_idm(self.summary["best"])
        raw = load_match(self.paths[0])
        port = 1 if raw.port(1)["input_valid"].any() else 2
        stripped = self._strip(self.paths[0], port)
        before = stripped.read_bytes()
        res = label_match(model, ck, stripped, port, log=lambda s: None)
        self.assertEqual(stripped.read_bytes(), before, "input file must not change")
        out = Path(res["path"])
        self.assertTrue(out.name.endswith(".inferred.npz"))
        self.assertEqual(out.parent, stripped.parent)

        m = load_match(out)
        n = len(m.frames)
        self.assertEqual(m.header["inferred_inputs"][str(port)]["rows"], int(m.inferred[:, port - 1].sum()))
        self.assertGreater(res["rows"], n * 0.9)
        self.assertFalse(m.inferred[:, 2 - port].any())
        self.assertEqual(int(m.port(port)["input_valid"].sum()), 0)  # still "unknown" to anything that ignores the flag
        self.assertEqual(json.loads(out.with_suffix(".json").read_text())["derived_from"], m.header["derived_from"])

        # inferred inputs are mostly right where the raw log had them
        both = m.inferred[:, port - 1] & (raw.port(port)["input_valid"] == 1)

        def enc(p):
            return np.stack(A.encode_arrays(p["stick_x"], p["stick_y"], p["cstick_x"], p["cstick_y"], p["buttons"]), 1)

        agree = (enc(m.port(port)[both]) == enc(raw.port(port)[both])).all(1).mean()
        self.assertGreater(agree, 0.6)

        # dataset: excluded by default, included on request
        cfg = FeatureConfig(bot_port=port)
        ds_off = build_dataset([out], cfg)
        ds_on = build_dataset([out], cfg, include_inferred=True)
        self.assertEqual(len(ds_off.sample_rows()), 0)
        self.assertGreater(len(ds_on.sample_rows()), n * 0.8)
        # a raw log and its labeled copy are not double counted
        self.assertEqual(len(build_dataset([stripped, out], cfg).match_ids), 1)
        self.assertEqual(len(build_dataset([stripped, out], cfg, include_inferred=True).match_ids), 1)

    def test_refuses_to_overwrite_input(self):
        from training.idm import label_match, load_idm
        model, ck = load_idm(self.summary["best"])
        with self.assertRaises(SystemExit):
            label_match(model, ck, self.paths[0], 1, out=self.paths[0], log=lambda s: None)
        with self.assertRaises(SystemExit):
            label_match(model, ck, self.paths[0], 1, out=self.dir / "x.npz", log=lambda s: None)


if __name__ == "__main__":
    unittest.main()
