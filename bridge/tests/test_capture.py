"""SSB-13 input capture (state packet v3/v4), bot port threading, bot input
logging from acks, and bridge.run + logger + eval in one process."""
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from bridge import buttons
from bridge.eval import EvalSession
from bridge.fake_switch import human_inputs
from bridge.fake_switch import run as fake_run
from bridge.logger import MatchLogger, frame_at, load_match
from bridge.matches import MatchWatcher
from bridge.protocol import Frame, Inputs
from bridge.run import BotInputRecorder, run as bridge_run
from bridge.scripted import ScriptedRobPolicy
from bridge.udp_listen import describe_inputs
from bridge.wire import (
    STATE_LEN,
    ControlStatus,
    Header,
    pack_ack,
    pack_control_status,
    pack_heartbeat,
    pack_state,
    unpack_ack,
    unpack_control_status,
    unpack_state,
)

from .helpers import fighter, frame
from .test_fields import rust_fighter

# plugin/src/net.rs tests::state_v4_golden (printed by the host test crate).
RUST_STATE_V4_HEX = (
    "534350550400010009000000d2040000010000002d00000000004841000050400000c0bf"
    "0000304000002a42000016420300000047000000000000002301efcdab00000000008040"
    "ffffffff01000000030000002d000000000048c1000050400000c03f0000304000002041"
    "000016420300000047000000000000002301efcdab00000000008040ffffffff01000000"
    "030000003b0000007f80002805000000c00000000800010064fb810041000000007f0000"
    "10000000"
)
P1_IN = Inputs(127, -128, 0, 40, buttons.ATTACK | buttons.JUMP)
P2_IN = Inputs(-64, 0, 0, 0, buttons.GUARD | buttons.GUARD_HOLD)
CM1_IN = Inputs(100, -5, -127, 0, buttons.ATTACK | buttons.JUMP_MINI)
CM2_IN = Inputs(0, 127, 0, 0, buttons.CATCH)


def golden_state() -> Frame:
    return Frame(1234, 1, rust_fighter(42.5, 1.0), rust_fighter(10.0, -1.0),
                 inputs=(P1_IN, P2_IN), driven=(False, True), cm_inputs=(CM1_IN, CM2_IN))


class WireTests(unittest.TestCase):
    def test_v4_matches_rust(self):
        self.assertEqual(STATE_LEN, 184)
        self.assertEqual(pack_state(9, golden_state()).hex(), RUST_STATE_V4_HEX)
        _, f = unpack_state(bytes.fromhex(RUST_STATE_V4_HEX))
        self.assertEqual(f, golden_state())
        self.assertEqual((f.bot_inputs, f.opponent_inputs), (P1_IN, P2_IN))
        self.assertEqual(f.cm_inputs, (CM1_IN, CM2_IN))

    def test_inputs_stay_in_port_order(self):
        _, f = unpack_state(bytes.fromhex(RUST_STATE_V4_HEX), bot_index=1)
        self.assertEqual(f.inputs, (P1_IN, P2_IN))
        self.assertEqual((f.bot_inputs, f.opponent_inputs), (P2_IN, P1_IN))
        self.assertEqual(f.driven, (False, True))

    def test_uncaptured_is_none(self):
        _, f = unpack_state(pack_state(0, Frame(3, 0, fighter(), fighter(), inputs=(None, P2_IN))))
        self.assertEqual(f.inputs, (None, P2_IN))

    def test_control_status_mapping(self):
        s = ControlStatus(Header(4, 5, 1), 2, 0, 16 | 1, other_player_idx=1)
        back = unpack_control_status(pack_control_status(1, s))
        self.assertEqual(back, s)
        self.assertTrue(back.fim_override)
        self.assertEqual(back.player_idx_by_port(), [1, 0])

    def test_listener_line(self):
        line = describe_inputs(P1_IN)
        self.assertIn("L(+127,-128)", line)
        self.assertIn("ATTACK JUMP", line)
        self.assertIn("[PC]", describe_inputs(P2_IN, True))


def feed(watcher, frames, t0=0.0, status=None):
    watcher.packet(pack_heartbeat(0, 1, "13.0.4"), t0)
    if status is not None:
        watcher.packet(pack_control_status(0, status), t0)
    t = t0
    for i, f in enumerate(frames):
        t = t0 + i / 60
        watcher.packet(pack_state(i, f), t)
    watcher.poll(t + 2.0)


class BotPortTests(unittest.TestCase):
    """Frame.bot is the bot everywhere; logs stay in port order."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, bot_port):
        seen = []

        class Spy:
            def on_frame(self, f, seq, now):
                seen.append(f)

        lg = MatchLogger(out_dir=self.dir / str(bot_port), echo=lambda m: None)
        w = MatchWatcher([Spy(), lg], bot_port=bot_port)
        # on the wire: port 1 at x=-10 with 1 stock, port 2 at x=+10 with 3
        wire = [Frame(i, 0, fighter(pos_x=-10.0, stocks=1), fighter(pos_x=10.0, stocks=3)) for i in range(5)]
        feed(w, wire)
        bot_x = -10.0 if bot_port == 1 else 10.0
        self.assertTrue(all(f.bot.pos_x == bot_x for f in seen))
        self.assertTrue(all(f.bot_index == bot_port - 1 for f in seen))
        # the policy sees the bot as the bot, no double swap with SCPU_BOT_PORT
        self.assertEqual(ScriptedRobPolicy(stage="fd", bot_port=bot_port)._orient(seen[0])[0].pos_x, bot_x)
        m = load_match(next((self.dir / str(bot_port)).glob("*.npz")))
        self.assertEqual(float(m.port(1)["pos_x"][0]), -10.0)
        self.assertEqual(float(m.port(2)["pos_x"][0]), 10.0)
        self.assertEqual(m.header["winner_port"], 2)
        self.assertEqual([p["final_stocks"] for p in m.header["ports"]], [1, 3])

    def test_bot_port_1(self):
        self.check(1)

    def test_bot_port_2(self):
        self.check(2)


class LoggedInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_human_capture_round_trip_and_header(self):
        lg = MatchLogger(out_dir=self.dir, echo=lambda m: None)
        w = MatchWatcher([lg], bot_port=2)
        frames = [frame(i, fighter(), fighter()) for i in range(6)]
        frames = [Frame(f.frame, f.stage_id, f.bot, f.opponent,
                        inputs=(human_inputs(i, -1), None if i == 3 else human_inputs(i, 1)))
                  for i, f in enumerate(frames)]
        st = ControlStatus(Header(4, 5, 0), 2, 0, 16 | 1, other_player_idx=1)
        feed(w, frames, status=st)
        m = load_match(next(self.dir.glob("*.npz")))
        p1, p2 = m.port(1), m.port(2)
        for i in range(6):
            want = human_inputs(i, -1)
            self.assertEqual((int(p1["stick_x"][i]), int(p1["buttons"][i])), (want.stick_x, want.buttons))
        self.assertEqual(int(p1["input_valid"].sum()), 6)
        self.assertEqual(list(p2["input_valid"]), [1, 1, 1, 0, 1, 1])
        self.assertEqual(frame_at(m, 2).inputs, (human_inputs(2, -1), human_inputs(2, 1)))
        h = m.header
        self.assertTrue(h["inputs_present"])
        self.assertEqual([p["inputs_present"] for p in h["ports"]], [True, True])
        cap = h["input_capture"]
        self.assertEqual(cap["mapping"], {"source": "control_status", "bot_port": 2,
                                          "player_idx_by_port": [1, 0], "fim_override": True,
                                          "verified": False})
        self.assertEqual(cap["rows_captured"], [6, 5])
        self.assertEqual(h["schema_version"], 1)

    def test_cpu_port_gets_inputs_from_control_module_and_human_port_agreement(self):
        """Port 1 is a human (fim + control module), port 2 a CPU (control module only)."""
        lg = MatchLogger(out_dir=self.dir, echo=lambda m: None)
        w = MatchWatcher([lg], bot_port=2)
        n = 10
        frames = []
        for i in range(n):
            fim = human_inputs(i, -1)
            # control module agrees except on frames 0..1, where it says jump
            # instead of whatever the human did (a different button class)
            cm1 = fim if i >= 2 else Inputs(fim.stick_x, fim.stick_y, fim.cstick_x, fim.cstick_y, buttons.JUMP)
            cm2 = human_inputs(i, 1)
            frames.append(Frame(i, 1, fighter(), fighter(), inputs=(fim, None), cm_inputs=(cm1, cm2)))
        feed(w, frames)
        m = load_match(next(self.dir.glob("*.npz")))
        p1, p2 = m.port(1), m.port(2)
        self.assertEqual(list(p2["input_valid"]), [1] * n)
        for i in range(n):
            want = human_inputs(i, 1)
            self.assertEqual((int(p2["stick_x"][i]), int(p2["buttons"][i])), (want.stick_x, want.buttons))
        # the human port keeps the fim values, not the control module's
        self.assertEqual(int(p1["buttons"][0]), human_inputs(0, -1).buttons)
        h = m.header
        self.assertEqual([p["inputs_present"] for p in h["ports"]], [True, True])
        cap = h["input_capture"]
        self.assertEqual(cap["port_sources"], ["fim", "control_module"])
        self.assertEqual(cap["rows_captured"], [n, 0])
        self.assertEqual(cap["rows_control_module"], [n, n])
        self.assertEqual(cap["rows_from_control_module"], [0, n])
        a1, a2 = cap["control_module_agreement"]
        self.assertEqual(a1["rows_compared"], n)
        self.assertEqual(a1["all"], n - 2)  # frames 0 and 1 have an attack-class difference
        self.assertEqual(a1["all_pct"], 80.0)
        self.assertEqual(a2["rows_compared"], 0)

    def test_control_module_translation_round_trip(self):
        _, f = unpack_state(pack_state(1, Frame(5, 1, fighter(), fighter(), cm_inputs=(None, CM2_IN))))
        self.assertEqual(f.cm_inputs, (None, CM2_IN))
        self.assertEqual(f.inputs, (None, None))

    def test_no_control_status_says_so(self):
        lg = MatchLogger(out_dir=self.dir, echo=lambda m: None)
        feed(MatchWatcher([lg]), [frame(i) for i in range(3)])
        h = load_match(next(self.dir.glob("*.npz"))).header
        self.assertEqual(h["input_capture"]["mapping"]["source"], "none")
        self.assertFalse(h["inputs_present"])

    def test_acked_bot_inputs_land_on_applied_frame(self):
        lg = MatchLogger(out_dir=self.dir, echo=lambda m: None)
        rec = BotInputRecorder(lg, bot_port=2)
        w = MatchWatcher([rec, lg], bot_port=2)
        w.packet(pack_heartbeat(0, 1, "13.0.4"), 0.0)
        for i in range(10):
            w.packet(pack_state(i, frame(i)), i / 60)  # no capture: v3 tail empty
            rec.on_sent(i, Inputs(stick_x=i * 10))  # decided on frame i
        # delay 2: input decided on frame i took effect on i + 2. Frame 4's
        # input never got acked (repeat/neutral on frame 6): stays invalid.
        for i in range(7):
            if i != 4:
                rec.on_ack(unpack_ack(pack_ack(0, i, i + 2, i + 1)))
        rec.on_ack(unpack_ack(pack_ack(0, 99, 5, 5)))  # unknown for_frame: ignored
        w.poll(5.0)
        m = load_match(next(self.dir.glob("*.npz")))
        p2 = m.port(2)
        self.assertEqual(list(p2["input_valid"]), [0, 0, 1, 1, 1, 1, 0, 1, 1, 0])
        self.assertEqual([int(x) for x in p2["stick_x"][2:6]], [0, 10, 20, 30])
        self.assertEqual(int(p2["stick_x"][7]), 50)
        self.assertEqual(int(m.port(1)["input_valid"].sum()), 0)
        self.assertEqual(m.header["input_capture"]["rows_from_acks"], [0, 6])

    def test_capture_wins_over_acks_and_mismatch_is_counted(self):
        lg = MatchLogger(out_dir=self.dir, echo=lambda m: None)
        w = MatchWatcher([lg], bot_port=2)
        w.packet(pack_heartbeat(0, 1, "13.0.4"), 0.0)
        for i in range(4):
            w.packet(pack_state(i, Frame(i, 0, fighter(), fighter(), bot_index=1,
                                         inputs=(None, Inputs(stick_x=5)), driven=(False, True))), i / 60)
        lg.record_inputs(2, 1, Inputs(stick_x=5))   # agrees
        lg.record_inputs(2, 2, Inputs(stick_x=99))  # disagrees: capture wins
        w.poll(5.0)
        m = load_match(next(self.dir.glob("*.npz")))
        self.assertEqual([int(x) for x in m.port(2)["stick_x"]], [5, 5, 5, 5])
        self.assertEqual(m.header["input_capture"]["ack_capture_mismatches"], 1)


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class OneProcessTests(unittest.TestCase):
    """bridge.run playing, logging and scoring against the fake Switch."""

    def test_run_log_eval_three_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            port = free_port()
            session = EvalSession(games=3, model="scripted", bot_port=2, stage="battlefield",
                                  min_seconds=1.0, results_csv=tmp / "results.csv", data_dir=tmp,
                                  echo=lambda m: None)
            box = {}
            runner = threading.Thread(target=lambda: box.update(b=bridge_run(
                port, ScriptedRobPolicy(stage="battlefield"), 2, 2, "13.0.4", report_every=999,
                seconds=20, session=session, bind="127.0.0.1")))
            runner.start()
            time.sleep(0.2)
            fake_run("127.0.0.1", port, 0.0, "13.0.4", True, 20, bot_port=2, quiet=True,
                     matches=3, match_seconds=1.5, gap=1.8, winners=(2, 2, 1))
            runner.join(10)
            self.assertFalse(runner.is_alive())
            self.assertTrue(session.done)
            rows = (tmp / "results.csv").read_text().strip().splitlines()
            self.assertEqual(len(rows), 2)  # header + one row
            self.assertIn(",3,2,", rows[1])  # 3 games, 2 wins
            logs = sorted((tmp / "matches").glob("*.npz"))
            self.assertEqual(len(logs), 3)
            for path in logs:
                m = load_match(path, expect_version="13.0.4")
                h = m.header
                self.assertEqual([p["inputs_present"] for p in h["ports"]], [True, True])
                self.assertEqual([p["controller"] for p in h["ports"]], ["cpu:9", "bot:scripted"])
                cap = h["input_capture"]
                self.assertEqual(cap["mapping"]["player_idx_by_port"], [0, 1])
                self.assertGreater(cap["rows_from_acks"][1], 60)
                self.assertEqual(cap["ack_capture_mismatches"], 0)
                # v4: control module block on both ports, same as the fim capture in the fake
                self.assertGreater(min(cap["rows_control_module"]), 60)
                self.assertEqual(cap["port_sources"], ["fim", "ack+fim"])
                self.assertEqual([x["all_pct"] for x in cap["control_module_agreement"]], [100.0, 100.0])
                # human side is exactly what the fake's "controller" produced
                fr = m.frames["frame"]
                p1 = m.port(1)
                for row in (0, 30, len(fr) - 1):
                    want = human_inputs(int(fr[row]), -1)
                    self.assertEqual((int(p1["stick_x"][row]), int(p1["buttons"][row])),
                                     (want.stick_x, want.buttons))


if __name__ == "__main__":
    unittest.main()
