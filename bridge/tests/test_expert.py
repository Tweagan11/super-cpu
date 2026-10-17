"""SSB-18 (DAgger): CAP_BOT_EXPERT on the wire, expert columns in the logs,
old logs still loading, and fake_switch --expert through bridge.run."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from bridge.fake_switch import run as fake_run
from bridge.inspect_log import summarize
from bridge.logger import PLAYER_DTYPE, MatchLogger, load_match
from bridge.matches import MatchWatcher
from bridge.protocol import CAP_BOT_EXPERT, Frame, Inputs
from bridge.run import run as bridge_run
from bridge.scripted import ScriptedRobPolicy
from bridge.wire import pack_heartbeat, pack_state, unpack_state

from .helpers import fighter
from .test_capture import free_port

PLAYED = Inputs(stick_x=10, buttons=1)
AI = Inputs(stick_x=-90, stick_y=5, buttons=2)
OPP = Inputs(stick_x=3)


def expert_frame(i, expert=True):
    return Frame(i, 0, fighter(), fighter(), bot_index=1, inputs=(OPP, PLAYED), driven=(False, True),
                 cm_inputs=(OPP, AI), cm_expert=expert)


class ExpertWireTests(unittest.TestCase):
    def test_flag_round_trip(self):
        buf = pack_state(1, expert_frame(7))
        _, f = unpack_state(buf, bot_index=1)
        self.assertTrue(f.cm_expert)
        self.assertEqual(f.cm_inputs, (OPP, AI))
        self.assertEqual(f.expert_inputs, (None, AI))
        self.assertEqual(f.inputs, (OPP, PLAYED))
        self.assertEqual(CAP_BOT_EXPERT, 64)

    def test_no_flag_no_expert(self):
        _, f = unpack_state(pack_state(1, expert_frame(7, expert=False)), bot_index=1)
        self.assertFalse(f.cm_expert)
        self.assertEqual(f.expert_inputs, (None, None))

    def test_flag_without_driven_port_means_nothing(self):
        f = Frame(0, 0, fighter(), fighter(), inputs=(OPP, OPP), driven=(False, False),
                  cm_inputs=(OPP, OPP), cm_expert=True)
        self.assertEqual(f.expert_inputs, (None, None))


class ExpertLoggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def log(self, flags):
        lg = MatchLogger(out_dir=self.dir, echo=lambda m: None)
        w = MatchWatcher([lg], bot_port=2)
        w.packet(pack_heartbeat(0, 1, "13.0.4"), 0.0)
        for i, ex in enumerate(flags):
            w.packet(pack_state(i, expert_frame(i, ex)), i / 60)
        w.poll(5.0)
        return load_match(next(self.dir.glob("*.npz")))

    def test_columns_and_header(self):
        m = self.log([False, True, True, True, False])
        p2 = m.port(2)
        self.assertEqual(list(p2["expert_valid"]), [0, 1, 1, 1, 0])
        self.assertEqual(int(p2["expert_stick_x"][1]), -90)
        self.assertEqual(int(p2["expert_buttons"][2]), 2)
        self.assertEqual(int(p2["expert_stick_x"][0]), 0)
        # played inputs stay in the normal columns, from the driven capture
        self.assertEqual(list(p2["stick_x"]), [10] * 5)
        self.assertEqual(list(p2["input_valid"]), [1] * 5)
        self.assertEqual(int(m.port(1)["expert_valid"].sum()), 0)
        cap = m.header["input_capture"]
        self.assertEqual(cap["rows_expert"], [0, 3])
        self.assertIn("rows_expert 3", summarize(m))
        # expert rows are not read as a CM stream: no fill, no agreement stats
        self.assertEqual(cap["rows_control_module"][1], 2)
        self.assertEqual(cap["control_module_agreement"][1]["rows_compared"], 2)

    def test_old_log_loads_with_expert_invalid(self):
        m = self.log([True, True])
        old_names = [n for n in PLAYER_DTYPE.names if not n.startswith("expert_")]
        old = np.zeros(m.players.shape, dtype=[(n, PLAYER_DTYPE[n]) for n in old_names])
        for n in old_names:
            old[n] = m.players[n]
        path = next(self.dir.glob("*.npz"))
        old_path = self.dir / "old.npz"
        import json
        h = dict(m.header)
        h.pop("input_capture")
        np.savez_compressed(old_path, header=np.array(json.dumps(h)), frames=m.frames, players=old)
        o = load_match(old_path)
        self.assertEqual(o.players.dtype, PLAYER_DTYPE)
        self.assertEqual(int(o.players["expert_valid"].sum()), 0)
        self.assertEqual(list(o.port(2)["stick_x"]), [10, 10])
        self.assertEqual(path.exists(), True)


class ExpertFakeSwitchTests(unittest.TestCase):
    def test_bridge_run_logs_expert(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            port = free_port()
            lg = MatchLogger(out_dir=tmp, controllers=("cpu:9", "bot:scripted"), echo=lambda m: None)
            runner = threading.Thread(target=lambda: bridge_run(
                port, ScriptedRobPolicy(stage="battlefield"), 2, 2, "13.0.4", report_every=999,
                seconds=20, logger=lg, bind="127.0.0.1", idle_timeout=3))
            runner.start()
            time.sleep(0.2)
            fake_run("127.0.0.1", port, 0.0, "13.0.4", True, 20, bot_port=2, quiet=True,
                     matches=1, match_seconds=2.0, gap=1.8, winners=(2,), expert=True)
            runner.join(15)
            logs = sorted(tmp.glob("**/*.npz"))
            self.assertEqual(len(logs), 1)
            m = load_match(logs[0])
            p2 = m.port(2)
            n = len(m.frames)
            rows = m.header["input_capture"]["rows_expert"]
            self.assertEqual(rows[0], 0)
            self.assertGreater(rows[1], 0.8 * n)
            ev = p2["expert_valid"] == 1
            both = ev & (p2["input_valid"] == 1)
            self.assertGreater(int(both.sum()), 0.8 * n)
            differ = (p2["expert_stick_x"] != p2["stick_x"]) & both
            self.assertGreater(int(differ.sum()), 0.2 * n)
            self.assertGreater(len(set(p2["expert_stick_x"][ev].tolist())), 5)


if __name__ == "__main__":
    unittest.main()
