import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from bridge.fake_switch import scripted_match
from bridge.inspect_log import summarize
from bridge.logger import (
    FRAME_DTYPE,
    PLAYER_DTYPE,
    LogRejected,
    MatchLogger,
    decide_winner,
    frame_at,
    load_match,
)
from bridge.matches import MatchWatcher
from bridge.protocol import Inputs
from bridge.wire import pack_heartbeat, pack_state, unpack_state

from .helpers import S, fighter, frame


def play(watcher, frames, t0=0.0, version="13.0.4"):
    """Feed one match of frames through a watcher, then let it time out."""
    watcher.packet(pack_heartbeat(0, 1, version), t0)
    t = t0
    for i, f in enumerate(frames):
        t = t0 + i / 60
        watcher.packet(pack_state(i, f), t)
    watcher.poll(t + 2.0)


class LoggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.lines = []

    def tearDown(self):
        self.tmp.cleanup()

    def logger(self, **kw):
        kw.setdefault("echo", self.lines.append)
        return MatchLogger(out_dir=self.dir, **kw)

    def test_round_trip(self):
        lg = self.logger(stage="battlefield", controllers=("cpu:9", "bot:scripted"))
        frames = scripted_match(300, winner=2, winner_losses=1)
        play(MatchWatcher([lg]), frames)
        files = sorted(self.dir.glob("*.npz"))
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].with_suffix(".json").exists())

        m = load_match(files[0], expect_version="13.0.4", expect_mod_hash="unset")
        self.assertEqual(m.frames.dtype, FRAME_DTYPE)
        self.assertEqual(m.players.dtype, PLAYER_DTYPE)
        self.assertEqual(m.players.shape, (300, 2))
        # Every logged row decodes back to exactly what came off the wire
        # (the wire is f32, so compare against a decoded packet, not the source floats).
        for row in (0, 123, 299):
            self.assertEqual(frame_at(m, row), unpack_state(pack_state(row, frames[row]))[1])

        h = m.header
        self.assertEqual(h["game_version"], "13.0.4")
        self.assertEqual(h["frame_count"], 300)
        self.assertEqual(h["end_reason"], "state_stopped")
        self.assertEqual(h["winner_port"], 2)
        self.assertEqual(h["stage_layout"], "battlefield")
        self.assertEqual(h["stage_source"], "flag")
        self.assertEqual(h["ruleset"], {"stocks": 3, "items": False, "time_limit_min": None})
        self.assertFalse(h["inputs_present"])
        self.assertEqual([p["fighter_name"] for p in h["ports"]], ["robot", "robot"])
        self.assertEqual([p["controller"] for p in h["ports"]], ["cpu:9", "bot:scripted"])
        self.assertEqual([p["stocks_lost"] for p in h["ports"]], [3, 1])
        self.assertEqual(json.loads(files[0].with_suffix(".json").read_text()), h)
        # Input columns exist and are zero-filled
        self.assertEqual(int(m.players["input_valid"].sum()), 0)
        self.assertEqual(int(np.abs(m.players["buttons"]).sum()), 0)
        self.assertIn("inputs: absent", summarize(m))

    def test_wrong_version_writes_nothing(self):
        lg = self.logger()
        play(MatchWatcher([lg]), scripted_match(120, 1, 0), version="13.0.3")
        self.assertEqual(list(self.dir.glob("*")), [])
        self.assertFalse(lg.last.accepted)
        self.assertTrue(any("13.0.3" in line and "NOT writing" in line for line in self.lines))

    def test_no_heartbeat_writes_nothing(self):
        lg = self.logger()
        w = MatchWatcher([lg])
        for i in range(10):
            w.packet(pack_state(i, frame(i)), i / 60)
        w.poll(5.0)
        self.assertEqual(list(self.dir.glob("*")), [])

    def test_load_rejects_mismatch(self):
        lg = self.logger()
        play(MatchWatcher([lg]), scripted_match(60, 1, 0))
        path = next(self.dir.glob("*.npz"))
        with self.assertRaises(LogRejected):
            load_match(path, expect_version="13.0.3")
        with self.assertRaises(LogRejected):
            load_match(path, expect_mod_hash="abc123")
        load_match(path.with_suffix(".json"))  # either file name works

    def test_two_matches_two_files(self):
        lg = self.logger()
        w = MatchWatcher([lg])
        play(w, scripted_match(90, 1, 0), t0=0.0)
        play(w, scripted_match(90, 2, 0), t0=10.0)
        self.assertEqual(len(list(self.dir.glob("*.npz"))), 2)

    def test_recorded_inputs_fill_columns(self):
        lg = self.logger()
        w = MatchWatcher([lg])
        w.packet(pack_heartbeat(0, 1, "13.0.4"), 0.0)
        for i in range(5):
            w.packet(pack_state(i, frame(i)), i / 60)
        lg.record_inputs(2, 3, Inputs(stick_x=-127, stick_y=64, buttons=0x5))
        w.poll(5.0)
        m = load_match(next(self.dir.glob("*.npz")))
        p2 = m.port(2)
        self.assertEqual(int(p2["stick_x"][3]), -127)
        self.assertEqual(int(p2["buttons"][3]), 5)
        self.assertEqual(int(p2["input_valid"].sum()), 1)
        self.assertEqual(int(m.port(1)["input_valid"].sum()), 0)
        self.assertTrue(m.header["inputs_present"])
        self.assertEqual([p["inputs_present"] for p in m.header["ports"]], [False, True])

    def test_winner_falls_back_to_dead_status(self):
        f = frame(0, fighter(stocks=1, status_kind=S["DEAD"]), fighter(stocks=1))
        self.assertEqual(decide_winner(f), (2, "dead_status"))
        self.assertEqual(decide_winner(frame(0)), (None, None))


if __name__ == "__main__":
    unittest.main()
