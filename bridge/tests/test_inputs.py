"""Input path (SSB-5/6/7): packet layouts, the fake Switch's input rules, and
the bridge end to end over localhost."""
import socket
import threading
import time
import unittest

from bridge import buttons
from bridge.fake_switch import REPEAT_FRAMES, FakeInputSide
from bridge.fake_switch import run as fake_run
from bridge.latency import measure, report
from bridge.policy import WalkTowardPolicy
from bridge.protocol import Fighter, Frame, Inputs
from bridge.run import AckStats, Bridge, percentile
from bridge.wire import (
    ACK_LEN,
    CONTROL_STATUS_LEN,
    INPUT_LEN,
    KIND_INPUT,
    BadPacket,
    ControlStatus,
    Header,
    bits_list,
    pack_ack,
    pack_control_status,
    pack_input,
    pack_state,
    unpack_ack,
    unpack_control_status,
    unpack_input,
    unpack_state,
)

# Same strings as GOLDEN_* in plugin/src/input.rs. If one side changes a
# layout, both test suites have to change.
GOLDEN_INPUT = "534350550400020007000000e8030000ea0300007f81050a05000000"
GOLDEN_ACK = "534350550400040009000000e8030000ea030000e9030000"
GOLDEN_STATUS = (
    "53435055040005000b000000" "02010700"
    "e8030000" "03000000" "e2030000" "de030000"
    "01000000" "02000000" "03000000" "04000000"
    "40060000" "06000000"
    "78880000" "04000000"
)


def fighter(x: float) -> Fighter:
    return Fighter(0x2D, x, 0.0, 0.0, 0.0, 0.0, 0.0, 3, 0, 0, 0.0, 1, 0, 0)


class LayoutTests(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(INPUT_LEN, 28)
        self.assertEqual(ACK_LEN, 24)
        self.assertEqual(CONTROL_STATUS_LEN, 64)

    def test_input_matches_rust(self):
        inp = Inputs(127, -127, 5, 10, buttons.ATTACK | buttons.JUMP)
        self.assertEqual(pack_input(7, 1000, 1002, inp).hex(), GOLDEN_INPUT)
        back = unpack_input(bytes.fromhex(GOLDEN_INPUT))
        self.assertEqual((back.header.seq, back.for_frame, back.apply_at, back.inputs), (7, 1000, 1002, inp))

    def test_input_clamps_sticks(self):
        p = unpack_input(pack_input(0, 1, 2, Inputs(300, -300, 0, 0, 0)))
        self.assertEqual((p.inputs.stick_x, p.inputs.stick_y), (127, -128))

    def test_ack_matches_rust(self):
        self.assertEqual(pack_ack(9, 1000, 1002, 1001).hex(), GOLDEN_ACK)
        a = unpack_ack(bytes.fromhex(GOLDEN_ACK))
        self.assertEqual((a.for_frame, a.applied_frame, a.ready_frame), (1000, 1002, 1001))
        self.assertEqual((a.delay, a.round_trip), (2, 1))

    def test_status_matches_rust(self):
        s = ControlStatus(
            Header(4, 5, 11), 2, 1, 7, fim_calls=1000, idx_seen=3, inputs_rx=994, applied=990,
            late=1, rejected=2, repeats=3, neutral=4, cpu_calls=1600, cpu_idx_seen=6,
            orig=Inputs(120, -120, 0, 0, 4),
        )
        self.assertEqual(pack_control_status(11, s).hex(), GOLDEN_STATUS)
        back = unpack_control_status(bytes.fromhex(GOLDEN_STATUS))
        self.assertEqual(back, s)
        self.assertTrue(back.hook_installed and back.session_active and back.receiving)
        self.assertFalse(back.cpu_probe)
        self.assertEqual(bits_list(back.idx_seen), [0, 1])
        self.assertEqual(bits_list(1 << 31 | 4), [2, "other"])

    def test_kinds_dont_cross(self):
        with self.assertRaises(BadPacket):
            unpack_input(bytes.fromhex(GOLDEN_ACK))
        with self.assertRaises(BadPacket):
            unpack_ack(bytes.fromhex(GOLDEN_INPUT))

    def test_bot_port_mapping(self):
        f = Frame(5, 0, fighter(1.0), fighter(2.0), bot_index=1)  # bot = 1.0, on port 2
        # bot on port 2: on the wire port 1 is the opponent
        buf = pack_state(0, f)
        _, as_p1 = unpack_state(buf)
        self.assertEqual((as_p1.bot.pos_x, as_p1.opponent.pos_x), (2.0, 1.0))
        self.assertEqual([x.pos_x for x in as_p1.by_port], [2.0, 1.0])
        _, back = unpack_state(buf, bot_index=1)
        self.assertEqual(back, f)
        self.assertEqual([x.pos_x for x in back.by_port], [2.0, 1.0])

    def test_button_bits_match_modpack(self):
        # Training Modpack src/common/input.rs, Buttons bitflags
        self.assertEqual((buttons.ATTACK, buttons.SPECIAL, buttons.JUMP, buttons.GUARD), (1, 2, 4, 8))
        self.assertEqual(buttons.SPECIAL_RAW2, 0x20000)


class FakeInputRulesTests(unittest.TestCase):
    """The fake Switch copies plugin/src/input.rs. Same scenarios as its tests."""

    def setUp(self):
        self.s = FakeInputSide()
        self.seq = 0

    def recv(self, for_frame, apply_at, x, now=100.0, next_target=None):
        if next_target is None:
            next_target = (self.s.last_target + 1) if self.s.last_target is not None else for_frame + 1
        r = self.s.receive(pack_input(self.seq, for_frame, apply_at, Inputs(stick_x=x)), now, next_target)
        self.seq += 1
        return r

    def x(self, frame, now=100.0):
        out, _ = self.s.step(frame, now)
        return None if out is None else out.stick_x

    def test_hands_off_without_session(self):
        self.assertIsNone(self.x(1))

    def test_late_repeat_neutral(self):
        self.x(0)  # nothing yet, session closed
        self.assertEqual(self.recv(0, 1, 77, next_target=1), "queued")
        self.assertEqual(self.x(1), 77)
        for f in range(2, 2 + REPEAT_FRAMES):
            self.assertEqual(self.x(f), 77)
        self.assertEqual(self.x(2 + REPEAT_FRAMES), 0)
        self.assertEqual(self.recv(5, 6, 1), "late")

    def test_timeout(self):
        self.recv(0, 1, 9, now=100.0, next_target=1)
        self.assertEqual(self.x(1, now=100.0), 9)
        self.assertEqual(self.x(2, now=101.9), 9)
        self.assertIsNone(self.x(3, now=102.0))

    def test_old_seq(self):
        self.recv(0, 2, 1, next_target=1)
        self.seq = 0
        self.assertEqual(self.recv(0, 3, 1), "old")


class PercentileTests(unittest.TestCase):
    def test_nearest_rank(self):
        v = list(range(1, 101))
        self.assertEqual(percentile(v, 50), 50)
        self.assertEqual(percentile(v, 99), 99)
        self.assertEqual(percentile([3], 99), 3)
        self.assertEqual(percentile([2] * 98 + [9, 9], 99), 9)

    def test_ack_stats_dedupes(self):
        st = AckStats()
        a = unpack_ack(pack_ack(0, 10, 12, 11))
        st.add(a)
        st.add(a)
        self.assertEqual(len(st), 1)


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class EndToEndTests(unittest.TestCase):
    """bridge.run's Bridge and bridge.latency against the fake Switch, over localhost."""

    def _fake(self, port, seconds, **kw):
        result = {}

        def go():
            result["side"] = fake_run("127.0.0.1", port, 0.0, "13.0.4", True, seconds, quiet=True, **kw)

        t = threading.Thread(target=go, daemon=True)
        t.start()
        return t, result

    def test_walk_policy_moves_the_bot(self):
        port = free_port()
        b = Bridge(port, WalkTowardPolicy(), delay=2, bot_port=2, bind="127.0.0.1", log=lambda m: None)
        t, result = self._fake(port, 2.5, bot_port=2)
        xs = []
        end = time.monotonic() + 3.5
        while time.monotonic() < end and t.is_alive():
            if b.poll(0.05) is not None and b.history._frames:
                xs.append(b.history._frames[-1])
        t.join()
        b.close()
        side = result["side"]
        self.assertGreater(b.inputs_sent, 60)
        self.assertGreater(side.c.applied, 60, "the fake Switch should apply most inputs")
        self.assertGreater(len(b.acks), 60)
        self.assertIsNotNone(b.status)
        self.assertEqual(b.status.bot_port, 2)
        # the bot walked toward the opponent: |dx| shrank
        first, last = xs[0], xs[-1]
        self.assertLess(abs(last.opponent.pos_x - last.bot.pos_x), abs(first.opponent.pos_x - first.bot.pos_x))
        # localhost: round trip is the 2-frame floor, maybe 3
        self.assertLessEqual(percentile(b.acks.round_trips, 50), 3)
        self.assertTrue(all(d == 2 for d in b.acks.delays))

    def test_latency_tool_sees_added_latency(self):
        port = free_port()
        # measure() binds right away; the fake starts sending a moment later
        box = {}
        m = threading.Thread(target=lambda: box.update(b=measure(port, 3.5, 10, 2, "127.0.0.1", True)))
        m.start()
        time.sleep(0.2)
        t, result = self._fake(port, 3.0, bot_port=2, latency_ms=50)
        t.join()
        m.join()
        b = box["b"]
        rt = b.acks.round_trips
        self.assertGreater(len(rt), 60)
        # 50 ms is 3 frames on top of the 2-frame floor
        med = percentile(rt, 50)
        self.assertGreaterEqual(med, 4)
        self.assertLessEqual(med, 7)
        text = report(b, 3.0)
        self.assertIn("p99", text)
        self.assertIn("late (Switch)  0", text)

    def test_late_inputs_are_counted(self):
        port = free_port()
        # 100 ms extra latency with delay 2 means everything is late
        b = Bridge(port, WalkTowardPolicy(), delay=2, bot_port=2, bind="127.0.0.1", log=lambda m: None)
        t, result = self._fake(port, 2.0, bot_port=2, latency_ms=100)
        end = time.monotonic() + 2.5
        while time.monotonic() < end and t.is_alive():
            b.poll(0.05)
        t.join()
        b.close()
        side = result["side"]
        self.assertGreater(side.c.late, 30)
        self.assertEqual(side.c.applied, 0)
        self.assertEqual(len(b.acks), 0)

    def test_bot_port_mismatch_warns(self):
        port = free_port()
        logs = []
        b = Bridge(port, WalkTowardPolicy(), delay=2, bot_port=1, bind="127.0.0.1", log=logs.append)
        t, _ = self._fake(port, 1.2, bot_port=2)
        end = time.monotonic() + 1.5
        while time.monotonic() < end and t.is_alive():
            b.poll(0.05)
        t.join()
        b.close()
        self.assertTrue(any("drives port 2" in m for m in logs), logs)

    def test_input_packets_reply_to_sender(self):
        # the bridge replies to the state's source address, not a fixed port
        port = free_port()
        b = Bridge(port, WalkTowardPolicy(), delay=2, bot_port=2, bind="127.0.0.1", log=lambda m: None)
        sw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sw.bind(("127.0.0.1", 0))
        sw.settimeout(1.0)
        # Frame(bot, opponent): bot at x=50, opponent at 0, bot on port 2
        sw.sendto(pack_state(0, Frame(41, 0, fighter(50.0), fighter(0.0)), bot_index=1), ("127.0.0.1", port))
        b.poll(1.0)
        buf, _ = sw.recvfrom(64)
        p = unpack_input(buf)
        self.assertEqual(p.header.kind, KIND_INPUT)
        self.assertEqual((p.for_frame, p.apply_at), (41, 43))
        self.assertLess(p.inputs.stick_x, 0, "walk left toward the opponent")
        sw.close()
        b.close()


if __name__ == "__main__":
    unittest.main()


class AckStatsTests(unittest.TestCase):
    def test_counts_again_after_frame_counter_restarts(self):
        from bridge.run import AckStats
        from bridge.wire import Ack, Header
        h = Header(4, 4, 0)
        st = AckStats()
        for f in range(1000, 2000):
            st.add(Ack(h, f, f + 3, f + 2))
        st.add(Ack(h, 1999, 2002, 2001))  # a real duplicate
        self.assertEqual(len(st), 1000)
        for f in range(0, 100):          # next match: the frame counter restarts
            st.add(Ack(h, f, f + 3, f + 2))
        self.assertEqual(len(st), 1100)


class WatchPolicyTests(unittest.TestCase):
    def test_watch_sends_nothing(self):
        from bridge.policy import POLICIES
        self.assertIsNone(POLICIES["watch"]().act([]))
