"""RL fine-tuning tests (SSB-21). Skipped when torch isn't installed."""
import csv
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

import training  # noqa: F401

try:
    import torch
except ImportError:  # pragma: no cover
    torch = None

from bridge.protocol import Fighter, Frame

ROBOT = 0x2D
SKIP = unittest.skipIf(torch is None, "torch not installed (pip install -e 'bridge[train]')")


def fighter(**kw) -> Fighter:
    base = dict(fighter_kind=ROBOT, pos_x=0.0, pos_y=0.0, vel_x=0.0, vel_y=0.0, damage=0.0, shield_hp=0.0,
                stocks=3, status_kind=0, motion_kind=0, anim_frame=0.0, facing=1, jumps_left=0, flags=0)
    base.update(kw)
    return Fighter(**base)


def fr(n, bot=None, opp=None, stage=0):
    return Frame(n, stage, bot or fighter(), opp or fighter(pos_x=50.0, facing=-1))


@SKIP
class RewardTests(unittest.TestCase):
    def tracker(self, **kw):
        from training.rl import RewardConfig, RewardTracker
        return RewardTracker(RewardConfig(**kw))

    def test_damage_and_stocks(self):
        t = self.tracker(offstage_penalty=0)
        out = [t.step(f) for f in [
            fr(0), fr(1),
            fr(2, opp=fighter(damage=10.0, stocks=3)),                          # dealt 10 -> +0.1
            fr(3, bot=fighter(damage=5.0), opp=fighter(damage=10.0)),           # took 5 -> -0.05
            fr(4, bot=fighter(damage=5.0), opp=fighter(damage=0.0, stocks=2)),  # opp stock (damage resets): +5
            fr(5, bot=fighter(damage=0.0, stocks=2), opp=fighter(stocks=2)),    # bot stock, hit 2 frames ago: -5
        ]]
        np.testing.assert_allclose(out, [0, 0, 0.1, -0.05, 5.0, -5.0], atol=1e-6)
        self.assertEqual((t.totals["stocks_taken"], t.totals["stocks_lost"], t.totals["self_destructs"]), (1, 1, 0))

    def test_stocks_read_zero_at_start(self):
        t = self.tracker()
        z, one = fighter(stocks=0), fighter(stocks=1)
        out = [t.step(f) for f in [fr(0, z, z), fr(1, z, z), fr(2, z, one), fr(3, one, one), fr(4, one, one)]]
        self.assertEqual(out, [0.0] * 5)  # 0 -> 1 is not a gain and nothing was lost
        self.assertEqual(t.totals["stocks_taken"] + t.totals["stocks_lost"], 0)
        # losing the last stock afterwards counts (and no damage taken for ages: self destruct)
        self.assertAlmostEqual(t.step(fr(5, z, one)), -5.0 - 2.0)

    def test_self_destruct_needs_no_recent_damage(self):
        t = self.tracker(self_destruct_window=10, offstage_penalty=0)
        out = [t.step(f) for f in [fr(0), fr(1, bot=fighter(damage=30.0)), fr(2, bot=fighter(damage=30.0, stocks=2))]]
        self.assertAlmostEqual(out[2], -5.0)  # hit one frame ago: just a KO
        t = self.tracker(self_destruct_window=10, offstage_penalty=0)
        out = [t.step(f) for f in [fr(i) for i in range(20)] + [fr(20, bot=fighter(stocks=2))]]
        self.assertAlmostEqual(out[-1], -7.0)
        self.assertEqual(t.totals["self_destructs"], 1)

    def test_offstage_grace_then_penalty(self):
        t = self.tracker(offstage_grace=3, offstage_penalty=0.1)
        off = fighter(pos_x=500.0)
        out = [t.step(f) for f in [fr(0)] + [fr(i, bot=off) for i in range(1, 7)] + [fr(7), fr(8, bot=off)]]
        self.assertEqual([round(x, 3) for x in out], [0, 0, 0, 0, -0.1, -0.1, -0.1, 0, 0])
        self.assertEqual(t.totals["offstage_frames"], 3)


@SKIP
class CreditTests(unittest.TestCase):
    def test_delayed_rewards_land_on_the_frame_that_applied(self):
        from training.rl import delayed_rewards
        frames = list(range(10))
        rew = [0.0] * 10
        rew[6] = 1.0
        self.assertEqual(list(np.flatnonzero(delayed_rewards(frames, frames, rew, delay=3))), [3])
        self.assertEqual(list(np.flatnonzero(delayed_rewards(frames, frames, rew, delay=3, lag=1))), [2])
        self.assertEqual(list(np.flatnonzero(delayed_rewards(frames, frames, rew, delay=0))), [6])

    def test_end_of_match_rewards_are_not_lost(self):
        from training.rl import delayed_rewards
        frames = list(range(10))
        r = delayed_rewards(frames, frames, [0.0] * 9 + [2.0], delay=3)
        self.assertAlmostEqual(float(r.sum()), 2.0)
        self.assertAlmostEqual(float(r[6]), 2.0)  # the decision applied on frame 9
        self.assertTrue(np.all(r[7:] == 0))

    def test_skipped_frames_keep_their_reward(self):
        from training.rl import delayed_rewards
        frames = [0, 1, 2, 5, 6, 7, 8]  # 3 and 4 never arrived
        r = delayed_rewards(frames, frames, [0, 0, 0, 1.0, 0, 0, 0], delay=2)
        self.assertAlmostEqual(float(r.sum()), 1.0)

    def test_gae_matches_hand_computation(self):
        from training.rl import gae
        r = np.array([0, 0, 1.0], np.float32)
        v = np.array([0.5, 0.4, 0.3], np.float32)
        adv, ret = gae(r, v, gamma=0.9, lam=0.8)
        a2 = 1.0 - 0.3
        a1 = 0.9 * 0.3 - 0.4 + 0.9 * 0.8 * a2
        a0 = 0.9 * 0.4 - 0.5 + 0.9 * 0.8 * a1
        np.testing.assert_allclose(adv, [a0, a1, a2], atol=1e-6)
        np.testing.assert_allclose(ret, adv + v, atol=1e-6)

    def test_gae_with_delay_credits_the_deciding_action(self):
        from training.rl import delayed_rewards, gae
        n, delay = 30, 3
        frames = list(range(n))
        rew = np.zeros(n)
        rew[20] = 1.0
        adv, _ = gae(delayed_rewards(frames, frames, rew, delay), np.zeros(n, np.float32), gamma=1.0, lam=1.0)
        # every decision up to frame 17 (applied by 20) earns the +1, later ones nothing
        np.testing.assert_allclose(adv[:18], 1.0)
        np.testing.assert_allclose(adv[18:], 0.0)


def tiny_bc_checkpoint(path, hidden=32, max_delay=3, bot_port=1):
    from training.features import FeatureConfig
    from training.train_bc import BCNet, ModelConfig, save_checkpoint
    torch.manual_seed(0)
    net = BCNet(FeatureConfig(bot_port=bot_port), ModelConfig(hidden=hidden))
    save_checkpoint(Path(path), net, {"max_delay": max_delay, "seed": 0})
    return net


def read_csv(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@SKIP
class PolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.bc = cls.root / "bc.pt"
        tiny_bc_checkpoint(cls.bc)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def make(self, name, **kw):
        from training.rl import RewardConfig, RLConfig, RLPolicy
        kw.setdefault("async_learn", False)
        kw.setdefault("matches_per_update", 1)
        kw.setdefault("epochs", 2)
        kw.setdefault("minibatch", 256)
        kw.setdefault("lr", 1e-3)
        kw.setdefault("value_warmup_updates", 0)
        kw.setdefault("min_decisions", 50)
        kw.setdefault("out_dir", str(self.root / "data" / name))
        kw.setdefault("ckpt_dir", str(self.root / "ck" / name))
        cfg = RLConfig(run=name, **kw)
        return RLPolicy(cfg, RewardConfig(), checkpoint=self.bc, log=lambda s: None)

    def play(self, p, winner=1, n=240):
        """One scripted match through the policy, with the watcher's events."""
        from bridge.fake_switch import scripted_match
        from bridge.matches import MatchEnded, MatchStarted
        from bridge.policy import History
        frames = scripted_match(n, winner, winner_losses=0, stocks=1)
        h = History()
        p.on_match_start(MatchStarted(frames[0], 0.0))
        for f in frames:
            p.act(h.push(f))
        p.on_match_end(MatchEnded(frames[-1], "state_stopped", 1.0))

    def weights(self, p):
        return torch.cat([x.flatten() for x in p.model.net.state_dict().values() if x.dtype.is_floating_point])

    def test_updates_between_matches_and_hot_swap(self):
        p = self.make("a")
        w0 = self.weights(p).clone()
        self.play(p, winner=1)                       # bot (port 1 in these frames) wins
        self.assertEqual(p.learner.updates, 1)
        self.assertTrue(torch.equal(self.weights(p), w0), "acting weights must not change mid-flight")
        self.play(p, winner=2)                       # the swap happens at this match's start
        self.assertEqual(p.version, 1)
        self.assertFalse(torch.equal(self.weights(p), w0))
        self.assertEqual([r["result"] for r in p.stats], ["win", "loss"])
        self.assertGreater(p.stats[0]["return"], 0)
        self.assertLess(p.stats[1]["return"], 0)
        rows = read_csv(p.out_dir / "matches.csv")
        self.assertEqual(len(rows), 2)
        ups = read_csv(p.out_dir / "updates.csv")
        self.assertEqual(len(ups), 2)
        self.assertGreater(float(ups[0]["kl_ref"]), -1e-6)

    def test_value_warmup_leaves_the_policy_alone(self):
        p = self.make("w", value_warmup_updates=1)
        net0 = {k: v.clone() for k, v in p.learner.model.net.state_dict().items()}
        self.play(p)
        for k, v in p.learner.model.net.state_dict().items():
            self.assertTrue(torch.equal(v, net0[k]), k)
        self.assertNotEqual(float(p.learner.model.value.weight.abs().sum()), 0.0)
        self.play(p)                                 # second update moves the policy
        moved = any(not torch.equal(v, net0[k]) for k, v in p.learner.model.net.state_dict().items())
        self.assertTrue(moved)

    def test_kl_penalty_pulls_toward_bc(self):
        def kl_after(coef):
            p = self.make(f"k{coef}", kl_coef=coef, ent_coef=0.0, epochs=6, lr=3e-3, target_kl=10.0)
            for _ in range(3):
                self.play(p)
            return p.learner.last_stats["kl_ref"]
        self.assertLess(kl_after(50.0), kl_after(0.0))

    def test_checkpoints_best_and_resume(self):
        from training.train_bc import load_checkpoint
        p = self.make("c", ckpt_every=2, best_min_matches=2, win_window=4)
        for w in (1, 1, 2):
            self.play(p, winner=w)
        p.close()
        self.assertTrue((p.ckpt_dir / "last.pt").exists())
        self.assertTrue((p.ckpt_dir / "best.pt").exists())
        model, ck = load_checkpoint(p.ckpt_dir / "best.pt")   # valid BC checkpoint too
        self.assertEqual(ck["max_delay"], 3)
        self.assertIn("optimizer", ck)
        q = self.make("c", resume=str(p.ckpt_dir), ckpt_dir=str(p.ckpt_dir), out_dir=str(p.out_dir))
        self.assertEqual(q.matches, p.matches)
        self.assertEqual(q.learner.updates, p.learner.updates)
        self.assertEqual(q.version, p.learner.updates)
        self.assertEqual(list(q.results), list(p.results))
        self.assertTrue(torch.equal(self.weights(q), p.learner and torch.cat(
            [x.flatten() for x in p.learner.model.net.state_dict().values() if x.dtype.is_floating_point])))
        self.play(q)
        self.assertEqual(q.learner.updates, p.learner.updates + 1)

    def test_bc_can_play_an_rl_checkpoint(self):
        from training.bc_policy import BCPolicy
        p = self.make("b", ckpt_every=1)
        self.play(p)
        p.close()
        bc = BCPolicy(p.ckpt_dir / "last.pt")
        from bridge.policy import History
        from bridge.fake_switch import scripted_match
        h = History()
        for f in scripted_match(30, 1, 0, stocks=1):
            bc.act(h.push(f))

    def test_short_or_interrupted_matches_are_discarded(self):
        from bridge.fake_switch import scripted_match
        from bridge.matches import MatchEnded, MatchStarted
        from bridge.policy import History
        p = self.make("d")
        frames = scripted_match(240, 1, 0, stocks=1)
        h = History()
        p.on_match_start(MatchStarted(frames[0], 0.0))
        for f in frames[:100]:
            p.act(h.push(f))
        p.on_match_end(MatchEnded(frames[99], "interrupted", 1.0))
        self.assertEqual(p.learner.updates, 0)
        self.assertEqual(p.matches, 0)
        self.assertEqual(p.stats[0]["kept"], 0)

    def test_stored_logprobs_match_the_update(self):
        """What act() records must equal what the learner recomputes, or PPO's ratio starts off wrong."""
        from bridge.fake_switch import scripted_match
        from bridge.policy import History
        from training.rl import _head_logp_ent_kl
        for temp in (1.0, 0.7):
            p = self.make(f"lp{temp}", temperature=temp)
            h = History()
            for f in scripted_match(60, 1, 0, stocks=1):
                p.act(h.push(f))
            cont = torch.from_numpy(np.stack(p._cont))
            cat = torch.from_numpy(np.stack(p._cat))
            with torch.no_grad():
                logits, v = p.model(cont, cat)
            logp, ent, _ = _head_logp_ent_kl(logits, None, torch.tensor(p._act), temp)
            np.testing.assert_allclose(logp.numpy(), p._logp, atol=1e-4)
            np.testing.assert_allclose(v.numpy(), p._val, atol=1e-5)
            self.assertAlmostEqual(float(ent.sum()), p._ent, places=2)

    def test_act_is_cheap(self):
        p = self.make("t")
        self.play(p)
        self.assertLess(p.stats[0]["act_ms_mean"], 5.0)  # ~0.7 ms normally; loose for slow CI

    def test_greedy_and_no_learn(self):
        p = self.make("g", greedy=True)
        self.assertIsNone(p.learner)
        self.play(p)
        self.play(p)
        self.assertFalse((p.out_dir / "matches.csv").exists())


@SKIP
class EndToEndTests(unittest.TestCase):
    """bridge.run's Bridge + --policy rl + the fake Switch with menus, several matches."""

    def test_matches_update_weights_and_no_late_inputs(self):
        from bridge.fake_switch import run as fake_run
        from bridge.policy import POLICIES
        from bridge.run import Bridge

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            tiny_bc_checkpoint(tmp / "bc.pt", bot_port=2)
            env = {"SCPU_CHECKPOINT": str(tmp / "bc.pt"), "SCPU_RL_RUN": "e2e", "SCPU_RL_OUT_DIR": str(tmp / "data"),
                   "SCPU_RL_CKPT_DIR": str(tmp / "ck"), "SCPU_RL_MATCHES_PER_UPDATE": "1", "SCPU_RL_EPOCHS": "2",
                   "SCPU_RL_VALUE_WARMUP_UPDATES": "0", "SCPU_RL_LR": "0.001", "SCPU_RL_MIN_DECISIONS": "30",
                   "SCPU_RL_CKPT_EVERY": "1"}
            old = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                policy = POLICIES["rl"]()
            finally:
                for k, v in old.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            w0 = torch.cat([x.flatten() for x in policy.model.net.state_dict().values() if x.dtype.is_floating_point]).clone()

            port = free_port()
            out = {}
            matches = 3

            def fake():
                out["side"] = fake_run("127.0.0.1", port, 0.0, "13.0.4", True, 60.0, quiet=True, matches=matches,
                                       match_seconds=2.0, menus=True)

            logs = []
            b = Bridge(port, policy, delay=2, bot_port=2, bind="127.0.0.1", log=logs.append, auto_rematch=True)
            b.rematch.settle = 0.2
            self.assertEqual(policy.delay, 2)               # configure(): the bridge's delay wins
            self.assertEqual(policy.fcfg.bot_port, 2)
            t = threading.Thread(target=fake, daemon=True)
            t.start()
            end = time.monotonic() + 60
            while time.monotonic() < end and t.is_alive():
                b.poll(0.02)
            t.join(1)
            b.close()                                       # policy.close(): waits for the learner, writes last.pt
            side = out["side"]

            self.assertEqual(side.scenes.count("MeleeScene"), matches, logs)
            self.assertEqual(side.c.late, 0, "no late inputs")
            self.assertGreater(side.c.applied, 3 * 100)
            self.assertEqual(len(policy.stats), matches)
            self.assertEqual([r["kept"] for r in policy.stats], [1] * matches)
            self.assertEqual(policy.learner.updates, matches)
            self.assertIsNone(policy.learner.error)
            self.assertGreaterEqual(policy.version, 1, "later matches played with updated weights")
            w1 = torch.cat([x.flatten() for x in policy.learner.model.net.state_dict().values() if x.dtype.is_floating_point])
            self.assertFalse(torch.equal(w0, w1), "weights changed")
            rows = read_csv(tmp / "data" / "matches.csv")
            self.assertEqual(len(rows), matches)
            self.assertEqual(len(read_csv(tmp / "data" / "updates.csv")), matches)
            self.assertTrue((tmp / "ck" / "last.pt").exists())


if __name__ == "__main__":
    unittest.main()
