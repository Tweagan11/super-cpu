import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

import training  # noqa: F401
from bridge.logger import LogRejected, frame_at, load_match
from bridge.protocol import Inputs
from training import actions as A
from training.features import (
    PER_SLOT,
    PREV_UNKNOWN,
    FeatureConfig,
    build_dataset,
    find_matches,
    frames_to_arrays,
    live_features,
    split_by_match,
)
from training.synthetic import synthetic_frames, write_synthetic_matches


def live_history(m, row, cfg):
    """What a live Policy would have been handed at this row: the last
    history_len received frames (bridge.policy.History)."""
    return [frame_at(m, r) for r in range(max(0, row - cfg.history_len + 1), row + 1)]


class FeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        # heavy drops and missing inputs so gaps show up all over the window
        cls.paths = write_synthetic_matches(cls.tmp.name, n_matches=3, n_frames=400, drop=0.15,
                                            no_input=0.1, seed=7)
        cls.matches = [load_match(p, expect_version="13.0.4") for p in cls.paths]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_logged_and_live_paths_identical(self):
        for cfg in (FeatureConfig(), FeatureConfig(window=8, prev_inputs=0), FeatureConfig(window=16, bot_port=2)):
            ds = build_dataset(self.paths, cfg, max_delay=4)
            self.assertGreater(int(ds.players.shape[0]), 900)
            offsets = np.cumsum([0] + [len(m.frames) for m in self.matches])
            rng = np.random.default_rng(0)
            # match starts, a spot right after a gap, and random rows, in every match
            for mi, m in enumerate(self.matches):
                fr = m.frames["frame"]
                gaps = np.flatnonzero(np.diff(fr) > 1)[:3] + 1
                local = sorted(set([0, 1, 5, 15, 16] + gaps.tolist() + rng.integers(0, len(fr), 10).tolist()))
                for r in local:
                    row = offsets[mi] + r
                    for d in range(5):
                        cont, cat = ds.batch(np.array([row]), np.array([d]))[:2] if ds.label_row[row, d] >= 0 else \
                            self._batch_no_label(ds, row, d)
                        lc, lk = live_features(live_history(m, r, cfg), ds.prev_inputs_for(row, d), d, cfg)
                        np.testing.assert_array_equal(cont[0], lc, err_msg=f"cfg={cfg} match {mi} row {r} d {d}")
                        np.testing.assert_array_equal(cat[0], lk)

    @staticmethod
    def _batch_no_label(ds, row, d):
        from training.features import window_features
        return window_features(ds.keys, ds.starts, ds.stage_ids, ds.players, np.array([row]), np.array([d]),
                               ds._prev(np.array([row]), np.array([d])), ds.cfg)

    def test_shapes_and_padding(self):
        cfg = FeatureConfig()
        ds = build_dataset(self.paths, cfg)
        cont, cat, tgt = ds.batch(np.array([0]), np.array([0]))
        self.assertEqual(cont.shape, (1, cfg.n_cont))
        self.assertEqual(cat.shape, (1, cfg.n_cat))
        self.assertEqual(len(cfg.cat_tables()), cfg.n_cat)
        slots = cont[0, :-1].reshape(cfg.window, PER_SLOT)
        pad_col = 2 * 15 + 3
        # first frame of a match: every slot but the newest is padding
        self.assertEqual(slots[:, pad_col].tolist(), [1.0] * (cfg.window - 1) + [0.0])
        # prev inputs are unknown at match start
        self.assertEqual(cat[0, -3:].tolist(), list(PREV_UNKNOWN))

    def test_gap_marks_age(self):
        m = self.matches[0]
        fr = m.frames["frame"]
        r = int(np.flatnonzero(np.diff(fr) > 1)[2]) + 1  # first row after a gap
        cfg = FeatureConfig()
        cont, _ = live_features(live_history(m, r, cfg), None, 0, cfg)
        slots = cont[:-1].reshape(cfg.window, PER_SLOT)
        age = slots[:, -1]
        self.assertEqual(age[-1], 0)            # newest frame is fresh
        self.assertGreater(age[-2], 0)          # the dropped frame reuses the last one we had
        self.assertEqual(slots[-2, :30].tolist(), slots[-2 - int(round(age[-2] * 10)), :30].tolist())

    def test_labels_and_delay_shift(self):
        ds = build_dataset(self.paths, FeatureConfig(), max_delay=4)
        m0 = self.matches[0]
        p = m0.port(1)
        fr = m0.frames["frame"]
        for i in range(50, 120):
            for d in range(5):
                j = np.flatnonzero(fr == fr[i] + d)
                lr = ds.label_row[i, d]
                if len(j) and p["input_valid"][j[0]]:
                    self.assertEqual(lr, j[0])
                    e = A.encode(Inputs(*(int(p[k][j[0]]) for k in ("stick_x", "stick_y", "cstick_x", "cstick_y", "buttons"))))
                    self.assertEqual(ds.labels[lr].tolist(), [e.main, e.cstick, e.buttons])
                else:
                    self.assertEqual(lr, -1)  # dropped frame or input_valid == 0: never a label
        # labels never cross into the next match
        last = len(m0.frames) - 1
        self.assertTrue((ds.label_row[last, 1:] == -1).all())
        # delays picked are always valid
        rows = ds.sample_rows()
        d = ds.pick_delays(rows, np.random.default_rng(0))
        self.assertTrue((ds.label_row[rows, d] >= 0).all())
        self.assertGreater(len(set(d.tolist())), 3)

    def test_prev_inputs_are_shifted_by_delay(self):
        ds = build_dataset(self.paths, FeatureConfig(prev_inputs=2), max_delay=4)
        m0 = self.matches[0]
        fr = m0.frames["frame"]
        i = 100
        for d in range(5):
            prev = ds.prev_inputs_for(i, d)
            for k, inp in zip((2, 1), prev):  # frames t+d-2, t+d-1
                j = np.flatnonzero(fr == fr[i] + d - k)
                if len(j) and m0.port(1)["input_valid"][j[0]]:
                    self.assertIsNotNone(inp)
                    self.assertEqual(A.encode(inp), A.ActionClasses(*ds.labels[j[0]].tolist()))
                else:
                    self.assertIsNone(inp)

    def test_live_stops_at_match_reset(self):
        cfg = FeatureConfig(window=8)
        import random
        old = synthetic_frames(30, random.Random(1), 0)
        new = synthetic_frames(3, random.Random(2), 0)
        hist = old[-13:] + new
        keys, starts, _, _ = frames_to_arrays(hist)
        self.assertEqual(starts[-1], 13)
        a, _ = live_features(hist, None, 0, cfg)
        b, _ = live_features(new, None, 0, cfg)
        np.testing.assert_array_equal(a, b)

    def test_rejects_wrong_version(self):
        with tempfile.TemporaryDirectory() as d:
            # the logger itself refuses wrong-version matches, so fake one by editing a copy's header
            src = np.load(self.paths[0])
            import json
            h = json.loads(str(src["header"]))
            h["game_version"] = "13.0.3"
            bad = Path(d) / "bad.npz"
            np.savez(bad, header=np.array(json.dumps(h)), frames=src["frames"], players=src["players"])
            with self.assertRaises(LogRejected):
                build_dataset([bad])
            build_dataset([bad], expect_version=None)  # explicitly opting out still works

    def test_split_by_match(self):
        tr, va = split_by_match(10, 0.2, seed=3)
        self.assertEqual(len(va), 2)
        self.assertFalse(set(tr) & set(va))
        self.assertEqual(sorted(tr + va), list(range(10)))
        self.assertEqual(split_by_match(10, 0.2, seed=3), (tr, va))
        ds = build_dataset(self.paths)
        rows_tr, rows_va = ds.sample_rows([0, 1]), ds.sample_rows([2])
        self.assertFalse(set(ds.match_index[rows_tr]) & set(ds.match_index[rows_va]))

    def test_config_bounds(self):
        for bad in (dict(window=7), dict(window=17), dict(bot_port=3)):
            with self.assertRaises(ValueError):
                FeatureConfig(**bad)

    def test_find_matches(self):
        self.assertEqual(sorted(find_matches([self.tmp.name])), sorted(Path(p) for p in self.paths))


if __name__ == "__main__":
    unittest.main()


class DatasetOrderTests(unittest.TestCase):
    def test_split_ignores_file_order(self):
        """Match file names have a random suffix, so the order files are listed
        in changes run to run. The dataset (and so the seeded train/val split)
        must not depend on it."""
        from training.features import build_dataset, split_by_match

        with tempfile.TemporaryDirectory() as tmp:
            paths = write_synthetic_matches(tmp, n_matches=5, n_frames=120, seed=3)
            a = build_dataset(list(paths))
            b = build_dataset(list(reversed(paths)))
        self.assertEqual(a.match_ids, b.match_ids)
        self.assertTrue(np.array_equal(a.labels, b.labels))
        tr_a, va_a = split_by_match(len(a.match_ids), 0.4, 0)
        tr_b, va_b = split_by_match(len(b.match_ids), 0.4, 0)
        self.assertEqual([a.match_ids[i] for i in va_a], [b.match_ids[i] for i in va_b])
