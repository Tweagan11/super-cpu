import unittest
from dataclasses import replace

from bridge import buttons as B
from bridge.policy import POLICIES, History
from bridge.scripted import ScriptedRobPolicy
from bridge.stages import BATTLEFIELD, FINAL_DESTINATION, STAGE_ID_LAYOUTS, layout_by_name, resolve_layout

from .helpers import S, airborne, fighter, frame


def act(policy, *frames):
    h = History()
    out = None
    for f in frames:
        out = policy.act(h.push(f))
    return out


class ScriptedTests(unittest.TestCase):
    def setUp(self):
        self.p = ScriptedRobPolicy(stage="fd")

    def test_registered(self):
        self.assertIsInstance(POLICIES["scripted"](), ScriptedRobPolicy)

    def test_offstage_below_recovers_toward_center(self):
        me = airborne(fighter(pos_x=110.0, pos_y=-30.0))
        out = act(self.p, frame(0, me))
        self.assertTrue(self.p.last_reason.startswith("recover"))
        self.assertLess(out.stick_x, 0)  # center is to the left
        self.assertEqual(out.buttons, B.JUMP)  # double jump first

        # Next frame: jump is spent, so up-special with stick up
        h = History()
        h.push(frame(0, me))
        out = self.p.act(h.push(frame(1, replace(me, status_kind=S["FALL_AERIAL"]))))
        self.assertEqual(self.p.last_reason, "recover: up-b")
        self.assertTrue(out.buttons & B.SPECIAL)
        self.assertGreater(out.stick_y, 0)
        self.assertLess(out.stick_x, 0)
        # Once Robo Burner is going, keep holding special
        out = self.p.act(h.push(frame(2, replace(me, status_kind=S["SPECIAL_HI"]))))
        self.assertTrue(out.buttons & B.SPECIAL)
        out = self.p.act(h.push(frame(3, replace(me, status_kind=S["SPECIAL_HI"]))))
        self.assertTrue(out.buttons & B.SPECIAL)

    def test_recovery_left_side_steers_right(self):
        out = act(self.p, frame(0, airborne(fighter(pos_x=-120.0, pos_y=-10.0))))
        self.assertGreater(out.stick_x, 0)

    def test_offstage_above_stage_is_not_recovery(self):
        act(self.p, frame(0, airborne(fighter(pos_x=110.0, pos_y=30.0))))
        self.assertFalse(self.p.last_reason.startswith("recover"))

    def test_close_and_opponent_attacking_shields(self):
        opp = fighter(pos_x=12.0, status_kind=S["ATTACK_S4"])
        out = act(self.p, frame(0, fighter(pos_x=0.0), opp))
        self.assertEqual(self.p.last_reason, "shield")
        self.assertEqual(out.buttons, B.GUARD)

    def test_close_and_opponent_specialing_shields(self):
        opp = fighter(pos_x=-15.0, status_kind=S["SPECIAL_S"])
        act(self.p, frame(0, fighter(), opp))
        self.assertEqual(self.p.last_reason, "shield")

    def test_low_shield_does_not_shield_when_shield_is_live(self):
        opp = fighter(pos_x=12.0, status_kind=S["ATTACK_S4"])
        act(self.p, frame(0, fighter(shield_hp=5.0), opp))
        self.assertNotEqual(self.p.last_reason, "shield")

    def test_close_not_attacking_attacks(self):
        out = act(self.p, frame(0, fighter(pos_x=0.0), fighter(pos_x=10.0)))
        self.assertEqual(self.p.last_reason, "attack: tilt")
        self.assertTrue(out.buttons & B.ATTACK)
        self.assertGreater(out.stick_x, 0)

    def test_attack_releases_between_presses(self):
        f = frame(0, fighter(pos_x=0.0), fighter(pos_x=10.0))
        first = act(self.p, f)
        second = self.p.act([f])
        self.assertTrue(first.buttons & B.ATTACK)
        self.assertFalse(second.buttons & B.ATTACK)

    def test_far_approaches(self):
        out = act(self.p, frame(0, fighter(pos_x=-40.0), fighter(pos_x=40.0)))
        self.assertEqual(self.p.last_reason, "approach")
        self.assertEqual(out.stick_x, B.STICK_MAX)
        self.assertEqual(out.buttons, B.NONE)
        out = act(self.p, frame(1, fighter(pos_x=40.0), fighter(pos_x=-40.0)))
        self.assertEqual(out.stick_x, -B.STICK_MAX)

    def test_does_not_walk_off_ledge(self):
        edge = FINAL_DESTINATION.ledge_x
        # Opponent high above, slightly further out: don't walk outward.
        out = act(self.p, frame(0, fighter(pos_x=edge - 5), fighter(pos_x=edge - 1, pos_y=50.0)))
        self.assertEqual(self.p.last_reason, "approach: at ledge")
        self.assertEqual(out.stick_x, 0)
        # Opponent offstage: don't chase, drift back to center.
        out = act(self.p, frame(1, fighter(pos_x=edge - 5), fighter(pos_x=edge + 30, pos_y=0.0)))
        self.assertEqual(self.p.last_reason, "neutral: back to center")
        self.assertLess(out.stick_x, 0)

    def test_dead_opponent_neutral(self):
        act(self.p, frame(0, fighter(), fighter(pos_x=50.0, status_kind=S["DEAD"])))
        self.assertTrue(self.p.last_reason.startswith("neutral"))

    def test_bot_port_two_swaps_sides(self):
        p = ScriptedRobPolicy(stage="fd", bot_port=2)
        # frame.bot is port 1 (opponent here), frame.opponent is port 2 (us)
        out = act(p, frame(0, fighter(pos_x=-40.0), fighter(pos_x=40.0)))
        self.assertEqual(out.stick_x, -B.STICK_MAX)

    def test_battlefield_platform_hop(self):
        p = ScriptedRobPolicy(stage="battlefield")
        out = act(p, frame(0, fighter(pos_x=10.0), fighter(pos_x=35.0, pos_y=27.0)))
        self.assertEqual(p.last_reason, "approach: hop to platform")
        self.assertEqual(out.buttons, B.JUMP)
        # FD has no platforms, same spot just walks
        act(self.p, frame(0, fighter(pos_x=10.0), fighter(pos_x=35.0, pos_y=27.0)))
        self.assertEqual(self.p.last_reason, "approach")

    def test_env_config(self):
        import os
        old = dict(os.environ)
        try:
            os.environ["SCPU_STAGE"] = "omega"
            os.environ["SCPU_BOT_PORT"] = "2"
            p = POLICIES["scripted"]()
            self.assertEqual(p.fallback_layout.name, "fd")
            self.assertEqual(p.cfg.bot_port, 2)
        finally:
            os.environ.clear()
            os.environ.update(old)


class StageTests(unittest.TestCase):
    def test_names(self):
        self.assertIs(layout_by_name("omega"), FINAL_DESTINATION)
        self.assertIs(layout_by_name("BF"), BATTLEFIELD)
        with self.assertRaises(ValueError):
            layout_by_name("smashville")

    def test_resolve_uses_stage_id_when_known(self):
        self.assertIs(resolve_layout(0, BATTLEFIELD), BATTLEFIELD)
        self.assertIs(resolve_layout(999, BATTLEFIELD), BATTLEFIELD)  # unknown id
        STAGE_ID_LAYOUTS[999] = "fd"
        try:
            self.assertIs(resolve_layout(999, BATTLEFIELD), FINAL_DESTINATION)
        finally:
            del STAGE_ID_LAYOUTS[999]


if __name__ == "__main__":
    unittest.main()
