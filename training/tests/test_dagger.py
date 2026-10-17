"""DAgger (SSB-18): expert labels in build_dataset and train_bc --dagger."""
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

import training  # noqa: F401
from bridge.logger import MatchLogger, load_match
from bridge.matches import MatchWatcher
from bridge.protocol import Fighter, Frame, Inputs
from bridge.wire import pack_heartbeat, pack_state

try:
    import torch
except ImportError:  # pragma: no cover
    torch = None

def fighter(**kw):
    base = dict(fighter_kind=0x2D, pos_x=0.0, pos_y=0.0, vel_x=0.0, vel_y=0.0, damage=0.0, shield_hp=0.0,
                stocks=3, status_kind=0, motion_kind=0, anim_frame=0.0, facing=1, jumps_left=0, flags=0)
    base.update(kw)
    return Fighter(**base)


def free_port():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


N = 60
PLAYED = [100, 0, -100]
EXPERT = [-100, 100, 0]


def bot_match(out_dir, expert_every=10, with_expert=True):
    """Bot on port 2: played stick_x cycles PLAYED, the CPU AI's decision
    cycles EXPERT (different class every frame). Expert missing on every
    `expert_every`-th row."""
    lg = MatchLogger(out_dir=out_dir, echo=lambda m: None)
    w = MatchWatcher([lg], bot_port=2)
    w.packet(pack_heartbeat(0, 1, "13.0.4"), 0.0)
    for i in range(N):
        has = with_expert and i % expert_every != 0
        f = Frame(i, 0, fighter(pos_x=-20.0 + i), fighter(pos_x=20.0, stocks=0 if i == N - 1 else 3), bot_index=1,
                  inputs=(None, Inputs(stick_x=PLAYED[i % 3])), driven=(False, True),
                  cm_inputs=(Inputs(), Inputs(stick_x=EXPERT[i % 3]) if has else Inputs(stick_x=PLAYED[i % 3])),
                  cm_expert=has)
        w.packet(pack_state(i, f), i / 60)
    w.poll(5.0)
    return next(Path(out_dir).glob("*.npz"))


@unittest.skipIf(torch is None, "torch not installed")
class DaggerDatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_expert_labels_only_where_valid_and_prev_is_played(self):
        from training import actions as A
        from training.features import FeatureConfig, build_dataset
        path = bot_match(self.dir / "bot")
        cfg = FeatureConfig(bot_port=2)
        plain = build_dataset([path], cfg, max_delay=0)
        dag = build_dataset([path], cfg, max_delay=0, expert_labels=True)
        main = lambda x: A.encode(Inputs(stick_x=x)).main
        # without the flag: played inputs, every row, no expert bookkeeping
        self.assertIsNone(plain.targets)
        self.assertEqual(int((plain.label_row[:, 0] >= 0).sum()), N)
        self.assertEqual(int(plain.labels[5, 0]), main(PLAYED[5 % 3]))
        # with it: expert class where expert_valid, nothing on the rest
        for i in range(N):
            if i % 10 == 0:
                self.assertEqual(int(dag.label_row[i, 0]), -1)
            else:
                self.assertEqual(int(dag.target_labels[dag.label_row[i, 0], 0]), main(EXPERT[i % 3]))
        self.assertEqual(int(dag.expert_row.sum()), N - N // 10)
        # prev inputs for row 5 at delay 0 are the PLAYED inputs of frames 1..4 (oldest first)
        prev = dag._prev(np.array([5]), np.array([0]))[0]
        want = [main(PLAYED[j % 3]) for j in range(5 - cfg.prev_inputs, 5)]
        self.assertEqual([int(c[0]) for c in prev], want)
        # batch targets are the expert's
        _, _, tgt = dag.batch(np.array([5]), np.array([0]))
        self.assertEqual(int(tgt[0, 0]), main(EXPERT[5 % 3]))
        # weights
        w = dag.sample_weights(np.array([5, 10]), np.array([0, 0]), 3.0)
        self.assertEqual(float(w[0]), 3.0)

    def test_cpu_matches_keep_input_labels(self):
        from training import actions as A
        from training.features import FeatureConfig, build_dataset
        cpu = bot_match(self.dir / "cpu", with_expert=False)  # no expert rows: an ordinary log
        bot = bot_match(self.dir / "bot")
        ds = build_dataset([cpu, bot], FeatureConfig(bot_port=2), max_delay=0, expert_labels=True)
        self.assertEqual(int(ds.expert_row.sum()), N - N // 10)
        # one match fully labeled (N rows), the other only its expert rows
        self.assertEqual(int((ds.label_row[:, 0] >= 0).sum()), N + N - N // 10)

    def test_train_dagger_end_to_end_from_fake_switch(self):
        from bridge.fake_switch import run as fake_run
        from bridge.run import run as bridge_run
        from bridge.scripted import ScriptedRobPolicy
        from training.features import FeatureConfig, find_matches
        from training.train_bc import ModelConfig, train
        logs = self.dir / "e2e"
        port = free_port()
        lg = MatchLogger(out_dir=logs, controllers=("cpu:9", "bot:bc"), echo=lambda m: None)
        t = threading.Thread(target=lambda: bridge_run(
            port, ScriptedRobPolicy(stage="battlefield"), 2, 2, "13.0.4", report_every=999,
            seconds=30, logger=lg, bind="127.0.0.1", idle_timeout=3))
        t.start()
        time.sleep(0.2)
        fake_run("127.0.0.1", port, 0.0, "13.0.4", True, 20, bot_port=2, quiet=True,
                 matches=2, match_seconds=2.0, gap=1.8, winners=(2, 1), expert=True)
        t.join(20)
        files = find_matches([logs])
        self.assertGreaterEqual(len(files), 2)
        self.assertGreater(load_match(files[0]).header["input_capture"]["rows_expert"][1], 50)
        logs_ = []
        s = train(files, fcfg=FeatureConfig(bot_port=2), mcfg=ModelConfig(hidden=32), epochs=2, val_frac=0.5,
                  out_dir=self.dir / "ckpt", log=logs_.append, dagger=True, expert_weight=2.0)
        self.assertTrue(Path(s["best"]).exists())
        self.assertTrue(any("dagger" in x for x in logs_))


if __name__ == "__main__":
    unittest.main()
