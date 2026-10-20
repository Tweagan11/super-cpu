"""RL fine-tuning tests (SSB-21). Skipped when torch isn't installed."""
import dataclasses
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


NO_SHAPING = dict(edgeguard_bonus=0.0, spike_bonus=0.0, combo_bonus=0.0, recovery_bonus=0.0,
                  last_jump_penalty=0.0, stage_control_weight=0.0, ledge_return_weight=0.0)


def fr(n, bot=None, opp=None, stage=0):
    return Frame(n, stage, bot or fighter(), opp or fighter(pos_x=50.0, facing=-1))


@SKIP
class RewardTests(unittest.TestCase):
    def tracker(self, **kw):
        from training.rl import RewardConfig, RewardTracker
        return RewardTracker(RewardConfig(**{**NO_SHAPING, **kw}))

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

    def test_self_destruct_judged_from_the_death_frame(self):
        # hit at frame 1, dies (DEAD) on frame 5, stock count only drops on frame 30:
        # a normal KO, though the stock change is more than the window after the hit
        t = self.tracker(self_destruct_window=10, offstage_penalty=0)
        frames = [fr(0), fr(1, bot=fighter(damage=30.0))]
        frames += [fr(i, bot=fighter(damage=30.0)) for i in range(2, 5)]
        frames += [fr(i, bot=fighter(damage=30.0, status_kind=181)) for i in range(5, 30)]
        frames += [fr(30, bot=fighter(stocks=2, status_kind=181))]
        out = [t.step(f) for f in frames]
        self.assertAlmostEqual(out[-1], -5.0)
        self.assertEqual(t.totals["self_destructs"], 0)
        # same timing with no hit for ages before dying: still a self-destruct
        t = self.tracker(self_destruct_window=10, offstage_penalty=0)
        frames = [fr(i) for i in range(20)] + [fr(i, bot=fighter(status_kind=181)) for i in range(20, 40)]
        frames += [fr(40, bot=fighter(stocks=2, status_kind=181))]
        out = [t.step(f) for f in frames]
        self.assertAlmostEqual(out[-1], -7.0)
        self.assertEqual(t.totals["self_destructs"], 1)

    def test_second_stock_ko_after_late_stock_drop_is_not_a_self_destruct(self):
        # first stock reaches 120%, KO, the stock count drops only after the
        # respawn; then the second stock takes 20% and is KO'd right after
        t = self.tracker(self_destruct_window=10, offstage_penalty=0)
        frames = [fr(0), fr(1, bot=fighter(damage=60.0)), fr(2, bot=fighter(damage=120.0))]
        frames += [fr(i, bot=fighter(damage=120.0, status_kind=181)) for i in range(3, 10)]
        frames += [fr(i, bot=fighter(damage=0.0)) for i in range(10, 30)]          # respawned, stocks still 3
        frames += [fr(30, bot=fighter(damage=0.0, stocks=2))]                        # late drop
        frames += [fr(i, bot=fighter(damage=0.0, stocks=2)) for i in range(31, 50)]
        frames += [fr(50, bot=fighter(damage=20.0, stocks=2))]                       # hit
        frames += [fr(i, bot=fighter(damage=20.0, stocks=2, status_kind=181)) for i in range(51, 60)]
        frames += [fr(60, bot=fighter(damage=0.0, stocks=1, status_kind=181))]
        [t.step(f) for f in frames]
        self.assertEqual(t.totals["stocks_lost"], 2)
        self.assertEqual(t.totals["self_destructs"], 0)
        self.assertAlmostEqual(t.totals["damage_taken"], 140.0)

    def test_offstage_grace_then_penalty(self):
        t = self.tracker(offstage_grace=3, offstage_penalty=0.1)
        off = fighter(pos_x=500.0)
        out = [t.step(f) for f in [fr(0)] + [fr(i, bot=off) for i in range(1, 7)] + [fr(7), fr(8, bot=off)]]
        self.assertEqual([round(x, 3) for x in out], [0, 0, 0, 0, -0.1, -0.1, -0.1, 0, 0])
        self.assertEqual(t.totals["offstage_frames"], 3)


HIT = 2        # Fighter.flags: in hitstun
AIR_HIT = 3    # airborne + in hitstun


def run(tracker, frames):
    return [tracker.step(f) for f in frames]


def renumber(frames, start=0):
    return [Frame(start + i, f.stage_id, f.bot, f.opponent) for i, f in enumerate(frames)]


@SKIP
class ShapingTests(unittest.TestCase):
    """Synthetic sequences on the Final Destination layout (stage 0 is unknown -> FD: center 0, ledge 79.9, ground 0)."""

    def only(self, term_weight: dict, **kw):
        """A tracker with the base reward off and just the named shaping term(s) on."""
        from training.rl import RewardConfig, RewardTracker
        base = dict(NO_SHAPING, damage_dealt=0.0, damage_taken=0.0, stock_taken=0.0, stock_lost=0.0,
                    self_destruct=0.0, offstage_penalty=0.0)
        gamma = kw.pop("gamma", 0.99)
        mult = kw.pop("mult", 1.0)
        return RewardTracker(RewardConfig(**{**base, **term_weight, **kw}), gamma=gamma, shaping_mult=mult)

    # -- 1. edgeguard ---------------------------------------------------------
    def edgeguard_seq(self, free=40, hit=True, bot_x=60.0, hitstun_whole_time=False, opp_x=100.0, opp_y=-5.0):
        """Opponent recovers offstage for `free` frames, (maybe) gets hit, then loses a stock."""
        opp_flags = AIR_HIT if hitstun_whole_time else 1
        frames = [fr(0), fr(1)]
        for _ in range(free):
            frames.append(fr(len(frames), bot=fighter(pos_x=bot_x), opp=fighter(pos_x=opp_x, pos_y=opp_y, flags=opp_flags)))
        if hit:
            frames.append(fr(len(frames), bot=fighter(pos_x=bot_x), opp=fighter(pos_x=opp_x, pos_y=opp_y, damage=20.0, flags=AIR_HIT)))
        dmg = 20.0 if hit else 0.0
        frames.append(fr(len(frames), bot=fighter(pos_x=bot_x), opp=fighter(pos_x=opp_x, pos_y=opp_y, damage=dmg, flags=AIR_HIT if hit else 1)))
        frames.append(fr(len(frames), bot=fighter(pos_x=bot_x), opp=fighter(stocks=2)))   # KO: respawned, damage reset
        return frames

    def test_edgeguard_ko_full_bonus_when_the_bot_hit_them_offstage(self):
        t = self.only(dict(edgeguard_bonus=2.0))
        out = run(t, self.edgeguard_seq())
        self.assertAlmostEqual(out[-1], 2.0)
        self.assertAlmostEqual(t.totals["edgeguard_reward"], 2.0)
        self.assertEqual(t.totals["edgeguards"], 1)

    def test_edgeguard_ledge_guarding_only_pays_half(self):
        t = self.only(dict(edgeguard_bonus=2.0))
        out = run(t, self.edgeguard_seq(hit=False, bot_x=60.0))      # onstage, 20 from the ledge
        self.assertAlmostEqual(out[-1], 1.0)

    def test_edgeguard_near_misses(self):
        def total(**kw):
            t = self.only(dict(edgeguard_bonus=2.0))
            run(t, self.edgeguard_seq(**kw))
            return t.totals["edgeguard_reward"]
        self.assertEqual(total(free=10), 0.0)                            # barely offstage before dying
        self.assertEqual(total(hit=False, bot_x=0.0), 0.0)               # recovering, died alone, bot far from the ledge
        self.assertEqual(total(opp_x=0.0, opp_y=0.0, bot_x=0.0), 0.0)    # never offstage (a plain KO, e.g. out the top)

    def test_edgeguard_needs_a_recovery_not_just_a_launch(self):
        t = self.only(dict(edgeguard_bonus=2.0))
        run(t, self.edgeguard_seq(hitstun_whole_time=True, hit=True, bot_x=60.0))   # in hitstun all the way out
        self.assertEqual(t.totals["edgeguards"], 0)

    def test_ledge_hanging_resets_the_recovery_clock(self):
        from training.rl import _CLIFF_STATUSES
        cliff = sorted(_CLIFF_STATUSES)[0]
        seq = self.edgeguard_seq(free=20)
        hang = [fr(0, bot=fighter(pos_x=60.0), opp=fighter(pos_x=82.0, pos_y=-3.0, status_kind=cliff)) for _ in range(5)]
        t = self.only(dict(edgeguard_bonus=2.0), edgeguard_min_frames=30)
        run(t, renumber(seq[:12] + hang + seq[12:]))      # 10 free frames, a hang, then 10 + the hit and the KO
        self.assertEqual(t.totals["edgeguards"], 0)
        t = self.only(dict(edgeguard_bonus=2.0), edgeguard_min_frames=30)
        run(t, renumber(self.edgeguard_seq(free=20)))     # same without the hang but too short: still nothing
        self.assertEqual(t.totals["edgeguards"], 0)

    # -- 2. spike ---------------------------------------------------------------
    def spike_seq(self, opp_x=100.0, opp_y=-5.0, vy_before=-1.0, vy_after=-4.0, vx_after=0.5, flags=AIR_HIT, extra=0):
        seq = [fr(0), fr(1)]
        seq += [fr(2 + i, opp=fighter(pos_x=opp_x, pos_y=opp_y, vel_y=vy_before, flags=1)) for i in range(3)]
        seq.append(fr(5, opp=fighter(pos_x=opp_x, pos_y=opp_y, vel_x=vx_after, vel_y=vy_after, damage=15.0, flags=flags)))
        for i in range(extra):
            seq.append(fr(6 + i, opp=fighter(pos_x=opp_x, pos_y=opp_y - 3 * i, vel_x=vx_after, vel_y=vy_after, damage=15.0, flags=flags)))
        return seq

    def spike_total(self, **kw):
        t = self.only(dict(spike_bonus=1.5))
        run(t, self.spike_seq(**kw))
        return t.totals["spike_reward"], t.totals["spikes"]

    def test_spike_fires_once_per_hit(self):
        self.assertEqual(self.spike_total(), (1.5, 1))
        self.assertEqual(self.spike_total(extra=4), (1.5, 1))      # still falling fast afterwards: no second payout

    def test_downward_hit_onstage_is_not_a_spike(self):
        self.assertEqual(self.spike_total(opp_x=0.0, opp_y=30.0), (0.0, 0))
        self.assertEqual(self.spike_total(opp_x=40.0, opp_y=0.0), (0.0, 0))

    def test_spike_near_misses_offstage(self):
        self.assertEqual(self.spike_total(vy_after=+3.0), (0.0, 0))                  # launched up
        self.assertEqual(self.spike_total(vy_after=-1.8), (0.0, 0))                  # weak, barely downward
        self.assertEqual(self.spike_total(vy_before=-4.0, vy_after=-4.0), (0.0, 0))  # was already falling that fast
        self.assertEqual(self.spike_total(vy_after=-3.0, vx_after=5.0), (0.0, 0))    # mostly sideways
        self.assertEqual(self.spike_total(flags=1), (0.0, 0))                        # not in hitstun

    def test_spike_velocity_may_show_up_a_frame_late(self):
        t = self.only(dict(spike_bonus=1.5))
        seq = self.spike_seq(vy_after=-1.0)                                           # the hit frame still shows old velocity
        seq.append(fr(6, opp=fighter(pos_x=100.0, pos_y=-6.0, vel_x=0.5, vel_y=-4.5, damage=15.0, flags=AIR_HIT)))
        run(t, seq)
        self.assertEqual(t.totals["spikes"], 1)

    # -- 3. combo ---------------------------------------------------------------
    def combo_seq(self, hit_frames, stun_between=True, bot_flags=0):
        out = [fr(0), fr(1)]
        dmg = 0.0
        for n in range(2, max(hit_frames) + 3):
            if n in hit_frames:
                dmg += 5.0
                out.append(fr(n, bot=fighter(flags=bot_flags), opp=fighter(damage=dmg, flags=HIT)))
            else:
                out.append(fr(n, opp=fighter(damage=dmg, flags=HIT if stun_between else 0)))
        return out

    def test_combo_pays_more_for_longer_combos(self):
        t = self.only(dict(combo_bonus=0.1))
        out = run(t, self.combo_seq([5, 15, 25, 35]))
        self.assertEqual([round(x, 3) for x in out if x], [0.1, 0.2, 0.3])       # hits 2, 3, 4
        self.assertEqual(t.totals["combo_hits"], 3)

    def test_combo_length_and_total_caps(self):
        t = self.only(dict(combo_bonus=0.1), combo_max_len=2)
        out = run(t, self.combo_seq([5, 15, 25, 35, 45]))
        self.assertEqual([round(x, 3) for x in out if x], [0.1, 0.2, 0.2, 0.2])
        t = self.only(dict(combo_bonus=0.1), combo_cap=0.25)
        run(t, self.combo_seq([5, 15, 25, 35]))
        self.assertAlmostEqual(t.totals["combo_reward"], 0.25)
        self.assertEqual(t.totals["combo_hits"], 3)                              # detections are still counted

    def test_combo_near_misses(self):
        def total(hits=(5, 15, 25), **kw):
            t = self.only(dict(combo_bonus=0.1))
            run(t, self.combo_seq(set(hits), **kw))
            return t.totals["combo_reward"]
        self.assertEqual(total(stun_between=False), 0.0)       # opponent left hitstun between hits: separate hits
        self.assertEqual(total(bot_flags=HIT), 0.0)            # trades are not combos
        self.assertEqual(total(hits=(5, 7, 9)), 0.0)           # one multi-hit move, hits 2 frames apart
        self.assertEqual(total(hits=(5,)), 0.0)                # a single hit

    # -- 4. recovery --------------------------------------------------------------
    def excursion(self, n_off, back_x=50.0, start=2):
        out = [fr(0), fr(1)]
        for i in range(n_off):
            out.append(fr(start + i, bot=fighter(pos_x=100.0, pos_y=-8.0, flags=1)))
        out.append(fr(start + n_off, bot=fighter(pos_x=back_x, pos_y=0.0)))
        return out

    def test_recovery_bonus(self):
        t = self.only(dict(recovery_bonus=0.5))
        out = run(t, self.excursion(40))
        self.assertAlmostEqual(out[-1], 0.5)
        self.assertEqual(t.totals["recoveries"], 1)

    def test_recovery_by_grabbing_the_ledge(self):
        from training.rl import _CLIFF_STATUSES
        cliff = sorted(_CLIFF_STATUSES)[0]
        t = self.only(dict(recovery_bonus=0.5))
        frames = self.excursion(40)[:-1] + [fr(42, bot=fighter(pos_x=82.0, pos_y=-3.0, status_kind=cliff))]
        self.assertAlmostEqual(run(t, frames)[-1], 0.5)

    def test_recovery_near_misses(self):
        t = self.only(dict(recovery_bonus=0.5))
        run(t, self.excursion(10))                                  # a hop off the edge
        self.assertEqual(t.totals["recoveries"], 0)
        t = self.only(dict(recovery_bonus=0.5))
        run(t, self.excursion(40)[:-1] + [fr(42, bot=fighter(pos_x=0.0, stocks=2))])   # never came back: lost the stock
        self.assertEqual(t.totals["recoveries"], 0)

    def test_recovery_cooldown_stops_ledge_drop_loops(self):
        t = self.only(dict(recovery_bonus=0.5), recovery_cooldown=500)
        run(t, self.excursion(40) + renumber(self.excursion(40), 100))
        self.assertEqual(t.totals["recoveries"], 1)

    def last_jump(self, x, y, before=1, after=0):
        t = self.only(dict(last_jump_penalty=0.3))
        out = run(t, [fr(0), fr(1), fr(2, bot=fighter(pos_x=x, pos_y=y, jumps_left=before)),
                      fr(3, bot=fighter(pos_x=x, pos_y=y, jumps_left=after))])
        return round(out[-1], 6), t.totals["last_jumps"]

    def test_last_jump_far_offstage_is_penalised(self):
        self.assertEqual(self.last_jump(140.0, -30.0), (-0.3, 1))

    def test_last_jump_near_misses(self):
        self.assertEqual(self.last_jump(85.0, -5.0), (0.0, 0))        # close to the ledge
        self.assertEqual(self.last_jump(20.0, 0.0), (0.0, 0))         # onstage
        self.assertEqual(self.last_jump(140.0, -30.0, before=2, after=1), (0.0, 0))   # not the last jump
        self.assertEqual(self.last_jump(140.0, -30.0, before=0, after=0), (0.0, 0))

    def test_self_destruct_penalty_stays(self):
        from training.rl import RewardConfig, RewardTracker
        t = RewardTracker(RewardConfig(self_destruct_window=10))
        run(t, [fr(i) for i in range(20)])
        t.step(fr(20, bot=fighter(stocks=2)))
        self.assertEqual(t.totals["self_destructs"], 1)

    # -- 5. potential-based stage control ---------------------------------------------
    def stage_seq(self, n=40):
        rng = np.random.default_rng(3)
        frames = [fr(0)]
        for i in range(1, n + 1):
            frames.append(fr(i, bot=fighter(pos_x=float(rng.uniform(-120, 120))), opp=fighter(pos_x=float(rng.uniform(-120, 120)))))
        return frames

    def test_potential_shaping_telescopes(self):
        for gamma, mult in ((0.9, 1.0), (0.99, 0.5)):
            t = self.only(dict(stage_control_weight=0.5), gamma=gamma, mult=mult)
            frames = self.stage_seq()
            out = run(t, frames)
            phi0, phiT = t.potential(frames[0]), t.potential(frames[-1])
            T = len(frames) - 1
            disc = sum(gamma ** (i - 1) * r for i, r in enumerate(out) if i >= 1)
            self.assertAlmostEqual(disc, gamma ** T * phiT - phi0, places=9)
            # with the terminal potential 0 the whole episode sums to -Phi(start)
            fin = t.finish()
            self.assertAlmostEqual(disc + gamma ** T * fin, -phi0, places=9)
            self.assertAlmostEqual(t.totals["stage_control_reward"], sum(out) + fin)

    def test_potential_prefers_the_bot_central_and_the_opponent_at_the_ledge(self):
        t = self.only(dict(stage_control_weight=1.0))
        good = t.potential(fr(0, bot=fighter(pos_x=0.0), opp=fighter(pos_x=75.0)))
        bad = t.potential(fr(0, bot=fighter(pos_x=75.0), opp=fighter(pos_x=0.0)))
        off = t.potential(fr(0, bot=fighter(pos_x=0.0), opp=fighter(pos_x=130.0)))
        self.assertGreater(good, 0)
        self.assertLess(bad, 0)
        self.assertAlmostEqual(off, 1.0)

    def test_stage_control_is_zero_while_nothing_moves_at_gamma_one(self):
        t = self.only(dict(stage_control_weight=0.5), gamma=1.0)
        out = run(t, [fr(i, opp=fighter(pos_x=70.0)) for i in range(10)])
        self.assertEqual(out, [0.0] * 10)

    # -- 4b. ledge return ------------------------------------------------------------
    def test_ledge_return_telescopes_and_skips_hitstun(self):
        t = self.only(dict(ledge_return_weight=0.01), ledge_return_cap=100.0)
        # walk out from 90 to 130 and back: nets 0
        xs = list(range(90, 131, 5)) + list(range(130, 89, -5))
        run(t, [fr(0), fr(1)] + [fr(2 + i, bot=fighter(pos_x=float(x))) for i, x in enumerate(xs)])
        self.assertAlmostEqual(t.totals["ledge_return_reward"], 0.0, places=6)
        # knocked out to 130 in hitstun (not counted), then back to 90: +0.4
        t = self.only(dict(ledge_return_weight=0.01), ledge_return_cap=100.0)
        out = [fr(0), fr(1)] + [fr(2 + i, bot=fighter(pos_x=float(x), flags=AIR_HIT)) for i, x in enumerate(range(90, 131, 5))]
        back = [fr(20 + i, bot=fighter(pos_x=float(x))) for i, x in enumerate(range(130, 89, -5))]
        run(t, out + back)
        self.assertAlmostEqual(t.totals["ledge_return_reward"], 0.4, places=5)
        self.assertGreater(t.totals["ledge_return_frames"], 0)

    def test_ledge_return_not_decayed_and_capped(self):
        t = self.only(dict(ledge_return_weight=0.01), ledge_return_cap=0.1, mult=0.2)
        frames = [fr(0), fr(1)] + [fr(2 + i, bot=fighter(pos_x=float(x), flags=AIR_HIT)) for i, x in enumerate(range(90, 131, 5))]
        frames += [fr(20 + i, bot=fighter(pos_x=float(x))) for i, x in enumerate(range(130, 89, -5))]
        run(t, frames)
        self.assertAlmostEqual(t.totals["ledge_return_reward"], 0.1, places=6)

    # -- decay and caps -----------------------------------------------------------------
    def test_shaping_multiplier_decays_linearly_to_the_floor(self):
        from training.rl import RewardConfig, shaping_multiplier
        c = RewardConfig(shaping_decay_matches=100, shaping_floor=0.2)
        self.assertEqual(shaping_multiplier(c, 0), 1.0)
        self.assertAlmostEqual(shaping_multiplier(c, 50), 0.6)
        self.assertAlmostEqual(shaping_multiplier(c, 100), 0.2)
        self.assertAlmostEqual(shaping_multiplier(c, 5000), 0.2)
        self.assertEqual(shaping_multiplier(RewardConfig(shaping_decay_matches=0), 999), 1.0)

    def test_decay_scales_shaping_but_not_the_base_reward(self):
        from training.rl import RewardConfig, RewardTracker
        for mult in (1.0, 0.5):
            t = RewardTracker(RewardConfig(**dict(NO_SHAPING, spike_bonus=1.5, offstage_penalty=0.0)), shaping_mult=mult)
            out = run(t, self.spike_seq())
            self.assertAlmostEqual(t.totals["spike_reward"], 1.5 * mult)
            self.assertAlmostEqual(sum(out), 0.01 * 15.0 + 1.5 * mult)     # the 15% damage is full weight

    def test_per_match_caps(self):
        t = self.only(dict(spike_bonus=1.5), spike_cap=2.0)
        for k in range(3):
            # each hit adds to the damage so far (damage never drops without a KO)
            seq = [Frame(f.frame, f.stage_id, f.bot, dataclasses.replace(f.opponent, damage=f.opponent.damage + 15.0 * k))
                   for f in self.spike_seq()]
            run(t, renumber(seq, 100 * k))
        self.assertAlmostEqual(t.totals["spike_reward"], 2.0)
        self.assertEqual(t.totals["spikes"], 3)
        t = self.only(dict(last_jump_penalty=0.3), last_jump_cap=0.5)
        for k in range(3):
            run(t, [fr(10 * k, bot=fighter(pos_x=140.0, pos_y=-30.0, jumps_left=1)),
                    fr(10 * k + 1, bot=fighter(pos_x=140.0, pos_y=-30.0, jumps_left=0))])
        self.assertAlmostEqual(t.totals["last_jump_reward"], -0.5)

    def test_env_var_sets_a_weight(self):
        from training.rl import RewardConfig, _from_env
        old = os.environ.get("SCPU_RL_REWARD_SPIKE_BONUS")
        os.environ["SCPU_RL_REWARD_SPIKE_BONUS"] = "3.5"
        try:
            self.assertEqual(_from_env(RewardConfig, "SCPU_RL_REWARD_").spike_bonus, 3.5)
        finally:
            if old is None:
                os.environ.pop("SCPU_RL_REWARD_SPIKE_BONUS")
            else:
                os.environ["SCPU_RL_REWARD_SPIKE_BONUS"] = old


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

    def make(self, name, reward=None, **kw):
        from training.rl import RewardConfig, RLConfig, RLPolicy
        kw.setdefault("async_learn", False)
        kw.setdefault("hold", 1)          # the original per-frame behaviour; action repeat has its own tests
        kw.setdefault("matches_per_update", 1)
        kw.setdefault("epochs", 2)
        kw.setdefault("minibatch", 256)
        kw.setdefault("lr", 1e-3)
        kw.setdefault("value_warmup_updates", 0)
        kw.setdefault("min_decisions", 50)
        kw.setdefault("out_dir", str(self.root / "data" / name))
        kw.setdefault("ckpt_dir", str(self.root / "ck" / name))
        cfg = RLConfig(run=name, **kw)
        return RLPolicy(cfg, reward or RewardConfig(), checkpoint=self.bc, log=lambda s: None)

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

    def test_matches_csv_has_per_term_columns_and_the_decay(self):
        from training.rl import MATCH_COLUMNS, RewardConfig, SHAPING_COLUMNS
        p = self.make("cols", reward=RewardConfig(shaping_decay_matches=2, shaping_floor=0.0))
        self.play(p)
        self.play(p)
        rows = read_csv(p.out_dir / "matches.csv")
        self.assertEqual(list(rows[0]), MATCH_COLUMNS)
        for col in ("edgeguard_reward", "spike_reward", "combo_reward", "recovery_reward", "last_jump_reward",
                    "stage_control_reward", "shaping_mult", "shaping_total", "edgeguards", "spikes", "combo_hits"):
            self.assertIn(col, rows[0])
        self.assertEqual(set(SHAPING_COLUMNS) - set(rows[0]), set())
        self.assertEqual([float(r["shaping_mult"]) for r in rows], [1.0, 0.5])

    def test_value_warmup_leaves_the_policy_alone(self):
        p = self.make("w", value_warmup_updates=1)
        net0 = {k: v.clone() for k, v in p.learner.model.net.state_dict().items()}
        self.play(p)
        for k, v in p.learner.model.net.state_dict().items():
            self.assertTrue(torch.equal(v, net0[k]), k)
        self.assertNotEqual(float(p.learner.model.value.weight.detach().abs().sum()), 0.0)
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
class ActionRepeatCreditTests(unittest.TestCase):
    def brute(self, dec, frames, rew, delay, lag=0):
        """Per decision: sum of rewards over [t_i+delay+lag, t_next+delay+lag)."""
        out = []
        for i, t in enumerate(dec):
            lo = t + delay + lag
            hi = dec[i + 1] + delay + lag if i + 1 < len(dec) else 10 ** 9
            out.append(sum(r for f, r in zip(frames, rew) if lo <= f < hi))
        return out

    def test_reward_is_the_sum_over_the_applied_window(self):
        from training.rl import window_rewards
        frames = list(range(14))
        rew = [0.1 * (i + 1) for i in range(14)]
        dec = [0, 2, 4, 6, 8, 10, 12]
        for delay, lag in ((0, 0), (3, 0), (2, 1)):
            got = window_rewards(dec, frames, rew, delay, lag)
            np.testing.assert_allclose(got, self.brute(dec, frames, rew, delay, lag), atol=1e-6)
        # delay 3: decision at 0 is applied on frames 3,4 -> rewards[3] + rewards[4]
        self.assertAlmostEqual(float(window_rewards(dec, frames, rew, 3)[0]), 0.4 + 0.5, places=6)

    def test_a_dropped_frame_keeps_its_reward_in_the_right_window(self):
        from training.rl import window_rewards
        frames = [0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11]   # 5 never arrived; frame 6 carries its reward
        rew = [0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 0]
        dec = [0, 2, 4, 6, 8, 10]
        got = window_rewards(dec, frames, rew, delay=1)
        np.testing.assert_allclose(got, self.brute(dec, frames, rew, 1), atol=1e-6)
        self.assertAlmostEqual(float(got.sum()), 1.0)
        self.assertEqual(int(np.flatnonzero(got)[0]), 2)   # applied window [5, 7) holds frame 6
        # a dropped *decision* frame: decisions at 0, 3 (frame 2 was lost), the window just gets longer
        got = window_rewards([0, 3], list(range(8)), [1.0] * 8, delay=0)
        np.testing.assert_allclose(got, [3.0, 5.0])

    def test_hold_one_is_the_original_credit(self):
        from training.rl import decision_rewards, delayed_rewards
        frames = [0, 1, 2, 5, 6, 7, 8]
        rew = [0, 0.5, 0, 1.0, 0, 2.0, 0]
        np.testing.assert_array_equal(decision_rewards(frames, frames, rew, 2, 0, hold=1),
                                      delayed_rewards(frames, frames, rew, 2, 0))

    def test_gae_over_decisions_discounts_per_decision_step(self):
        from training.rl import RLConfig, decision_gamma, gae
        cfg = RLConfig(hold=3, gamma=0.9)
        g = decision_gamma(cfg)
        self.assertAlmostEqual(g, 0.9 ** 3)
        self.assertEqual(decision_gamma(RLConfig(hold=1, gamma=0.9)), 0.9)
        r = np.zeros(6, np.float32)
        r[4] = 1.0
        adv, _ = gae(r, np.zeros(6, np.float32), g, lam=1.0)
        np.testing.assert_allclose(adv[:5], [g ** (4 - i) for i in range(5)], atol=1e-6)
        self.assertEqual(float(adv[5]), 0.0)
        # one decision step earlier is `hold` frames earlier: three frames of discount
        self.assertAlmostEqual(float(adv[3]) / float(adv[4]), 0.9 ** 3, places=6)


@SKIP
class ActionRepeatShapingTests(unittest.TestCase):
    def tracker(self, gamma, inline, mult=1.0):
        from training.rl import RewardConfig, RewardTracker
        cfg = RewardConfig(**dict(NO_SHAPING, damage_dealt=0.0, damage_taken=0.0, stock_taken=0.0, stock_lost=0.0,
                                  self_destruct=0.0, offstage_penalty=0.0, stage_control_weight=0.5))
        return RewardTracker(cfg, gamma=gamma, shaping_mult=mult, stage_inline=inline)

    def frames(self, n=41):
        rng = np.random.default_rng(5)
        return [fr(0)] + [fr(i, bot=fighter(pos_x=float(rng.uniform(-120, 120))),
                             opp=fighter(pos_x=float(rng.uniform(-120, 120)))) for i in range(1, n)]

    def test_potential_telescopes_over_decision_steps(self):
        hold, gframe = 4, 0.97
        gd = gframe ** hold
        t = self.tracker(gd, inline=False, mult=0.5)
        frames = self.frames()
        out = []
        for f in frames:
            self.assertEqual(t.step(f), 0.0)            # stage control is not paid per frame
            if f.frame % hold == 0:
                out.append(t.stage_step(f))
        dec = [f for f in frames if f.frame % hold == 0]
        phi0, phiT = t.potential(dec[0]), t.potential(dec[-1])
        T = len(dec) - 1
        disc = sum(gd ** (i - 1) * r for i, r in enumerate(out) if i >= 1)
        self.assertAlmostEqual(disc, gd ** T * phiT - phi0, places=9)
        fin = t.finish()
        self.assertAlmostEqual(disc + gd ** T * fin, -phi0, places=9)
        self.assertAlmostEqual(t.totals["stage_control_reward"], sum(out) + fin)

    def test_every_frame_stage_step_equals_the_inline_tracker(self):
        frames = self.frames()
        a, b = self.tracker(0.9, True), self.tracker(0.9, False)
        for f in frames:
            self.assertAlmostEqual(a.step(f), b.step(f) + b.stage_step(f), places=12)
        self.assertAlmostEqual(a.finish(), b.finish(), places=12)


@SKIP
class ActionRepeatPolicyTests(unittest.TestCase):
    setUpClass = classmethod(PolicyTests.setUpClass.__func__)
    tearDownClass = classmethod(PolicyTests.tearDownClass.__func__)
    make = PolicyTests.make
    play = PolicyTests.play

    def frames(self, n=240, winner=1):
        from bridge.fake_switch import scripted_match
        return scripted_match(n, winner, winner_losses=0, stocks=1)

    def drive(self, p, frames):
        from bridge.matches import MatchEnded, MatchStarted
        from bridge.policy import History
        eps = []
        p.learner.submit = lambda kind, payload: eps.append(payload) if kind == "episode" else None
        h = History()
        p.on_match_start(MatchStarted(frames[0], 0.0))
        for f in frames:
            p.act(h.push(f))
        p.on_match_end(MatchEnded(frames[-1], "state_stopped", 1.0))
        return eps

    def test_held_frames_return_the_same_inputs_and_record_nothing(self):
        from bridge.policy import History
        p = self.make("h1", hold=3)
        h = History()
        outs, sizes = [], []
        for f in self.frames(30):
            outs.append(p.act(h.push(f)))
            sizes.append((len(p._cont), len(p._cat), len(p._act), len(p._logp), len(p._val), len(p._frames)))
        for i in range(30):
            self.assertIs(outs[i], outs[i - i % 3])          # the decision's Inputs object, held
            self.assertEqual(sizes[i], (i // 3 + 1,) * 6)    # nothing added between decisions
        self.assertEqual(p._frames, list(range(0, 30, 3)))
        self.assertEqual(len(p._rf), 30)                     # but every frame's reward is tracked

    def test_dropped_frames_delay_the_next_decision(self):
        from bridge.policy import History
        p = self.make("h2", hold=2)
        h = History()
        for f in self.frames(20):
            if f.frame not in (2, 3):                        # the decision due on 2 happens on 4
                p.act(h.push(f))
        self.assertEqual(p._frames[:4], [0, 4, 6, 8])

    def test_hold_one_decides_every_frame_like_before(self):
        from bridge.policy import History
        p = self.make("h3", hold=1)
        h = History()
        for f in self.frames(20):
            p.act(h.push(f))
        self.assertEqual(p._frames, list(range(20)))

    def test_episode_credit_matches_the_window_sum(self):
        from training.rl import delayed_rewards, window_rewards
        for hold in (1, 2, 3):
            p = self.make(f"c{hold}", hold=hold)
            ep = self.drive(p, self.frames())[0]
            self.assertEqual(len(ep), len(p._frames))
            self.assertEqual(len(ep.cont), len(ep.logp))
            if hold == 1:
                want = delayed_rewards(p._frames, p._rf, p._rr, p.delay, 0)
            else:
                want = window_rewards(p._frames, p._rf, p._rr, p.delay, 0)
                self.assertEqual(p._frames, list(range(0, 240, hold)))
            np.testing.assert_allclose(ep.reward, want, atol=1e-6)
            self.assertAlmostEqual(float(ep.reward.sum()), float(p.stats[-1]["return"]), places=4)
            # nothing is lost beyond the lead-in before the first applied frame
            self.assertAlmostEqual(float(ep.reward.sum()), float(sum(p._rr[p.delay:])), places=3)
            self.assertEqual(p.stats[-1]["hold"], hold)
            self.assertEqual(p.stats[-1]["decisions"], len(p._frames))

    def test_update_runs_with_hold_two(self):
        p = self.make("u2", hold=2)
        self.play(p)
        self.assertEqual(p.learner.updates, 1)
        self.assertEqual(p.stats[0]["decisions"], 120)
        self.assertIsNone(p.learner.error)

    def test_stored_logprobs_match_the_update_with_hold(self):
        from bridge.policy import History
        from training.rl import _head_logp_ent_kl
        p = self.make("lp", hold=2)
        h = History()
        for f in self.frames(60):
            p.act(h.push(f))
        self.assertEqual(len(p._cont), 30)
        with torch.no_grad():
            logits, v = p.model(torch.from_numpy(np.stack(p._cont)), torch.from_numpy(np.stack(p._cat)))
        logp, _, _ = _head_logp_ent_kl(logits, None, torch.tensor(p._act), 1.0)
        np.testing.assert_allclose(logp.numpy(), p._logp, atol=1e-4)

    def test_held_frames_are_cheap(self):
        from bridge.policy import History
        p = self.make("cheap", hold=4)
        h = History()
        hist = [h.push(f) for f in self.frames(240)]
        held = []
        for i, hs in enumerate(hist):
            t0 = time.perf_counter()
            p.act(hs)
            if i % 4:
                held.append(time.perf_counter() - t0)
        self.assertLess(1e3 * float(np.median(held)), 0.5)   # tracker step only; the decision frame runs the net


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
                   "SCPU_RL_CKPT_EVERY": "1", "SCPU_RL_HOLD": "2"}
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
            self.assertEqual(policy.cfg.hold, 2)
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
            self.assertEqual({r["hold"] for r in rows}, {"2"})
            for r in rows:   # a decision every 2 frames
                self.assertAlmostEqual(int(r["decisions"]), (int(r["frames"]) + 1) / 2, delta=3)
            self.assertEqual(len(read_csv(tmp / "data" / "updates.csv")), matches)
            self.assertTrue((tmp / "ck" / "last.pt").exists())


if __name__ == "__main__":
    unittest.main()
