import unittest

from bridge.matches import (
    END_FRAME_RESET,
    END_INTERRUPTED,
    END_STATE_STOPPED,
    END_STOPPED_LONG,
    MatchBoundary,
    MatchEnded,
    MatchStarted,
    MatchSummary,
    MatchWatcher,
    VersionGate,
    DamageCounter,
)
from bridge.wire import pack_heartbeat, pack_state

from .helpers import fighter, frame


class BoundaryTests(unittest.TestCase):
    def test_timeout_ends_match(self):
        b = MatchBoundary()
        evs = b.on_frame(frame(0), 10.0)
        self.assertIsInstance(evs[0], MatchStarted)
        b.on_frame(frame(1, opp=fighter(stocks=0)), 10.016)  # opponent out of stocks: game over
        self.assertIsNone(b.poll(11.0))       # 1 s of silence isn't enough
        ev = b.poll(11.6)
        self.assertIsInstance(ev, MatchEnded)
        self.assertEqual(ev.reason, END_STATE_STOPPED)
        self.assertEqual(ev.final.frame, 1)
        self.assertIsNone(b.poll(20.0))       # only ends once
        self.assertFalse(b.in_match)

    def test_frame_reset_ends_and_starts(self):
        b = MatchBoundary()
        b.on_frame(frame(0), 0.0)
        b.on_frame(frame(500), 0.1)
        evs = b.on_frame(frame(0), 0.2)
        self.assertEqual([type(e) for e in evs], [MatchEnded, MatchStarted])
        self.assertEqual(evs[0].reason, END_FRAME_RESET)
        self.assertEqual(evs[0].final.frame, 500)

    def test_resume_after_timeout_is_a_new_match_even_without_reset(self):
        # e.g. the first frames of match 2 were dropped, so it starts at frame 3
        b = MatchBoundary()
        b.on_frame(frame(0), 0.0)
        b.poll(5.0)
        evs = b.on_frame(frame(3), 6.0)
        self.assertEqual([type(e) for e in evs], [MatchStarted])

    def test_close(self):
        b = MatchBoundary()
        self.assertIsNone(b.close(0.0))
        b.on_frame(frame(0), 0.0)
        self.assertEqual(b.close(0.1).reason, END_INTERRUPTED)


class PauseTests(unittest.TestCase):
    """State stops while the game is paused, too. With stocks left that's a
    pause: the match stays open and carries on when frames resume."""

    def setUp(self):
        import bridge.matches as m

        self._saved, m.PAUSE_TIMEOUT_S = m.PAUSE_TIMEOUT_S, 300.0

    def tearDown(self):
        import bridge.matches as m

        m.PAUSE_TIMEOUT_S = self._saved

    def test_pause_keeps_match_open_and_resumes(self):
        b = MatchBoundary()
        b.on_frame(frame(0), 0.0)
        b.on_frame(frame(1), 0.016)
        self.assertIsNone(b.poll(30.0))      # 30 s paused, stocks left on both
        self.assertTrue(b.paused and b.in_match)
        evs = b.on_frame(frame(2), 30.5)     # unpaused: same counter carries on
        self.assertEqual(evs, [])
        self.assertFalse(b.paused)
        b.on_frame(frame(3, opp=fighter(stocks=0)), 31.0)
        self.assertEqual(b.poll(33.0).reason, END_STATE_STOPPED)

    def test_new_match_after_a_pause_ends_the_old_one(self):
        b = MatchBoundary()
        b.on_frame(frame(500), 0.0)
        b.poll(10.0)                          # quit from the pause menu, say
        evs = b.on_frame(frame(0), 20.0)
        self.assertEqual([type(e) for e in evs], [MatchEnded, MatchStarted])
        self.assertEqual(evs[0].reason, END_FRAME_RESET)

    def test_very_long_stop_with_stocks_left_ends(self):
        b = MatchBoundary()
        b.on_frame(frame(0), 0.0)
        self.assertEqual(b.poll(301.0).reason, END_STOPPED_LONG)


class Recorder:
    def __init__(self):
        self.events = []

    def on_heartbeat(self, hb):
        self.events.append(("hb", hb.game_version))

    def on_match_start(self, ev):
        self.events.append(("start", ev.first.frame))

    def on_frame(self, f, seq, now):
        self.events.append(("frame", f.frame))

    def on_match_end(self, ev):
        self.events.append(("end", ev.reason))


class WatcherTests(unittest.TestCase):
    def test_packet_routing_order(self):
        r = Recorder()
        w = MatchWatcher([r, object()])  # observers without hooks are fine
        w.packet(pack_heartbeat(0, 1, "13.0.4"), 0.0)
        w.packet(pack_state(1, frame(0)), 0.0)
        w.packet(pack_state(2, frame(1)), 0.02)
        w.packet(pack_state(3, frame(0, opp=fighter(stocks=0))), 0.04)
        w.poll(2.0)
        self.assertEqual(r.events, [
            ("hb", "13.0.4"), ("start", 0), ("frame", 0), ("frame", 1),
            ("end", END_FRAME_RESET), ("start", 0), ("frame", 0), ("end", END_STATE_STOPPED),
        ])


class SummaryTests(unittest.TestCase):
    def test_damage_and_stocks(self):
        s = MatchSummary(frame(0))
        seq = [
            (fighter(damage=0), fighter(damage=0)),
            (fighter(damage=10), fighter(damage=0)),
            (fighter(damage=30), fighter(damage=5)),
            (fighter(damage=0, stocks=2), fighter(damage=5)),  # stock lost, damage resets
            (fighter(damage=12, stocks=2), fighter(damage=5)),
        ]
        for i, (a, b) in enumerate(seq):
            s.add(frame(i, a, b))
        self.assertEqual(s.damage_taken, [42.0, 5.0])
        self.assertEqual(s.stocks_lost, [1, 0])
        self.assertEqual(s.winner_port, 2)
        self.assertEqual(s.frame_count, 5)


    def test_flicker_and_garbage_damage_not_counted(self):
        s = MatchSummary(frame(0))
        seq = [0, 20, 0, 20, 0, 25, 5000, 25, 30, -1, 30]
        for i, d in enumerate(seq):
            s.add(frame(i, fighter(damage=d), fighter(damage=0)))
        self.assertEqual(s.damage_taken[0], 30.0)


class DamageCounterTests(unittest.TestCase):
    def test_counter(self):
        c = DamageCounter()
        self.assertEqual(c.update(0, False), 0.0)
        self.assertEqual(c.update(12, False), 12.0)
        self.assertEqual(c.update(0, False), 0.0)    # flicker down
        self.assertEqual(c.update(12, False), 0.0)   # back up: already counted
        self.assertEqual(c.update(500, False), 0.0)  # impossible jump
        self.assertEqual(c.update(15, False), 3.0)
        self.assertEqual(c.update(0, True), 0.0)     # new stock
        self.assertEqual(c.update(9, False), 9.0)
        self.assertEqual(c.total, 24.0)
        self.assertEqual(c.rejected, 1)


    def test_late_stock_drop_does_not_hide_next_stock_damage(self):
        c = DamageCounter()
        for d in (0, 50, 120):
            c.update(d, False)
        c.update(120, False, respawning=True)   # KO'd: dead
        c.update(0, False, respawning=True)     # respawn platform, damage reset
        self.assertEqual(c.update(0, False), 0.0)     # back in play at 0%, stock count not dropped yet
        self.assertEqual(c.update(10, False), 10.0)
        self.assertEqual(c.update(10, True), 0.0)     # stock count finally drops
        self.assertEqual(c.update(25, False), 15.0)
        self.assertEqual(c.total, 145.0)


class VersionGateTests(unittest.TestCase):
    def test_verdicts(self):
        g = VersionGate("13.0.4")
        g.start_match()
        self.assertFalse(g.verdict()[0])  # nothing seen
        g.heartbeat("13.0.4")
        g.start_match()
        self.assertEqual(g.verdict(), (True, "ok", "13.0.4"))
        g.heartbeat("13.0.3")
        ok, why, _ = g.verdict()
        self.assertFalse(ok)
        self.assertIn("changed", why)
        g.start_match()
        ok, why, ver = g.verdict()
        self.assertFalse(ok)
        self.assertEqual(ver, "13.0.3")

    def test_no_expectation_accepts_anything(self):
        g = VersionGate(None)
        g.heartbeat("12.0.0")
        g.start_match()
        self.assertTrue(g.verdict()[0])


if __name__ == "__main__":
    unittest.main()
