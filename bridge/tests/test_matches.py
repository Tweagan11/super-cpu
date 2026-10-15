import unittest

from bridge.matches import (
    END_FRAME_RESET,
    END_INTERRUPTED,
    END_STATE_STOPPED,
    MatchBoundary,
    MatchEnded,
    MatchStarted,
    MatchSummary,
    MatchWatcher,
    VersionGate,
)
from bridge.wire import pack_heartbeat, pack_state

from .helpers import fighter, frame


class BoundaryTests(unittest.TestCase):
    def test_timeout_ends_match(self):
        b = MatchBoundary()
        evs = b.on_frame(frame(0), 10.0)
        self.assertIsInstance(evs[0], MatchStarted)
        b.on_frame(frame(1), 10.016)
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
        w.packet(pack_state(3, frame(0)), 0.04)
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
