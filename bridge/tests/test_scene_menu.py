"""Scene reporting and menu presses (SSB-25): packet layouts, the rematch
rules, and `bridge.run --auto-rematch` against the fake Switch."""
import socket
import threading
import time
import unittest

from bridge import menu_buttons as MB
from bridge.fake_switch import run as fake_run
from bridge.matches import MatchWatcher
from bridge.policy import WalkTowardPolicy
from bridge.rematch import MAX_PRESSES, Rematch
from bridge.run import Bridge
from bridge.scenes import CSS, MATCH, OTHER, RESULTS, SceneNames
from bridge.wire import (
    MENU_LEN,
    NO_MENU_SEQ,
    SCENE_FLAG_MENU_HOOK,
    SCENE_FLAG_SCENE_HOOK,
    SCENE_LEN,
    BadPacket,
    pack_header,
    pack_menu,
    pack_scene,
    unpack_menu,
    unpack_scene,
)


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class PacketTests(unittest.TestCase):
    def test_scene_roundtrip_and_layout(self):
        b = pack_scene(5, "StageSelectScene", 3, SCENE_FLAG_SCENE_HOOK | SCENE_FLAG_MENU_HOOK, 7, 9)
        self.assertEqual(len(b), SCENE_LEN)
        self.assertEqual(SCENE_LEN, 60)
        # same bytes the plugin's scene_packet() test checks (plugin/src/scene.rs)
        self.assertEqual(b[6:8], (7).to_bytes(2, "little"))
        self.assertEqual(b[28:44], b"StageSelectScene")
        self.assertEqual(b[44:], bytes(16))
        s = unpack_scene(b)
        self.assertEqual((s.header.seq, s.changes, s.menu_applied, s.menu_last_seq, s.name),
                         (5, 3, 7, 9, "StageSelectScene"))
        self.assertTrue(s.menu_hook)

    def test_scene_name_is_cut_to_32_bytes(self):
        s = unpack_scene(pack_scene(0, "x" * 50, 1))
        self.assertEqual(len(s.name), 32)
        self.assertEqual(unpack_scene(pack_scene(0, "", 1)).menu_last_seq, NO_MENU_SEQ)

    def test_menu_roundtrip_and_layout(self):
        b = pack_menu(1, 9, MB.PLUS, 4)
        self.assertEqual(len(b), MENU_LEN)
        self.assertEqual(b[6:8], (8).to_bytes(2, "little"))
        self.assertEqual(b[12:16], (9).to_bytes(4, "little"))
        self.assertEqual(b[16:20], (0x40000).to_bytes(4, "little"))
        self.assertEqual(b[20:22], (4).to_bytes(2, "little"))
        m = unpack_menu(b)
        self.assertEqual((m.press_seq, m.mask, m.hold_frames), (9, MB.PLUS, 4))
        self.assertEqual(unpack_menu(pack_menu(0, 0, MB.A, 0)).hold_frames, 1, "hold is at least 1")
        self.assertEqual(unpack_menu(pack_menu(0, 0, MB.A, 5000)).hold_frames, 120)

    def test_wrong_kind_rejected(self):
        with self.assertRaises(BadPacket):
            unpack_scene(pack_menu(0, 0, MB.A))
        with self.assertRaises(BadPacket):
            unpack_menu(pack_header(7, 0) + bytes(12))

    def test_menu_bits_match_the_controller_bitfield(self):
        # field order of the game's ButtonBitfield (Training Modpack input.rs)
        self.assertEqual(MB.A, 1 << 5)
        self.assertEqual(MB.PLUS, 1 << 18)

    def test_watcher_returns_and_forwards_the_scene(self):
        seen = []

        class Obs:
            def on_scene(self, scene, now):
                seen.append((scene.name, now))

        w = MatchWatcher([Obs()])
        header, scene = w.packet(pack_scene(3, "CharaSelectScene", 2), 12.5)
        self.assertEqual((header.kind, scene.name), (7, "CharaSelectScene"))
        self.assertEqual(seen, [("CharaSelectScene", 12.5)])
        self.assertIs(w.scene, scene)


class SceneNameTests(unittest.TestCase):
    def test_classify(self):
        n = SceneNames()
        self.assertEqual(n.classify("CharaSelectScene"), CSS)
        self.assertEqual(n.classify("MeleeScene"), MATCH)
        self.assertEqual(n.classify("MeleeResultScene"), RESULTS)
        self.assertEqual(n.classify("TitleScene"), OTHER)
        self.assertEqual(SceneNames(css=("Foo",)).classify("xfoo"), CSS)


class Clock:
    def __init__(self):
        self.t = 100.0


class RematchTests(unittest.TestCase):
    def setUp(self):
        self.pressed = []
        self.logs = []
        self.r = Rematch(lambda m, h: self.pressed.append((m, h)), self.logs.append, settle=1.0)

    def scene(self, name, changes, now):
        self.r.on_scene(unpack_scene(pack_scene(0, name, changes)), now)

    def run_until(self, start, seconds, step=0.1):
        t = start
        while t < start + seconds:
            self.r.tick(t)
            t += step
        return t

    def test_no_presses_before_a_match_ends(self):
        self.scene("ResultScene", 1, 0.0)
        self.run_until(0.0, 5)
        self.assertEqual(self.pressed, [])

    def test_a_on_results_then_plus_on_css(self):
        self.r.on_match_start(None)
        self.scene("MeleeScene", 1, 0.0)
        self.r.tick(5.0)
        self.assertEqual(self.pressed, [], "never during the match")
        self.r.on_match_end(None)
        self.scene("ResultScene", 2, 10.0)
        self.r.tick(10.5)
        self.assertEqual(self.pressed, [], "waits for the scene to settle")
        self.r.tick(11.0)
        self.assertEqual(self.pressed, [(MB.A, 1)])
        self.r.tick(11.5)
        self.assertEqual(len(self.pressed), 1, "backs off before the next press")
        self.scene("CharaSelectScene", 3, 12.0)
        self.run_until(12.0, 1.2)
        self.assertEqual(self.pressed[-1], (MB.PLUS, 1))
        # the next match starts: back to waiting
        self.r.on_match_start(None)
        self.scene("MeleeScene", 4, 14.0)
        n = len(self.pressed)
        self.run_until(14.0, 5)
        self.assertEqual(len(self.pressed), n)

    def test_backoff_grows_and_gives_up(self):
        self.r.on_match_end(None)
        self.scene("ResultScene", 1, 0.0)
        times = []
        t = 0.0
        while t < 120.0:
            self.scene("ResultScene", 1, t)  # the 1 s scene packets keep arriving
            if self.r.tick(t):
                times.append(t)
            t += 0.1
        self.assertEqual(len(times), MAX_PRESSES)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(b >= a - 0.11 for a, b in zip(gaps, gaps[1:])), gaps)
        self.assertGreater(gaps[-1], gaps[0])
        self.assertTrue(any("giving up" in m for m in self.logs))

    def test_unknown_scene_is_logged_once_and_never_pressed(self):
        self.r.on_match_end(None)
        for i in range(5):
            self.scene("WeirdScene", 1, float(i))
        self.run_until(0.0, 10)
        self.assertEqual(self.pressed, [])
        self.assertEqual(sum("isn't a known scene" in m for m in self.logs), 1)

    def test_stale_scene_info_stops_presses(self):
        self.r.on_match_end(None)
        self.scene("ResultScene", 1, 0.0)
        self.r.tick(1.0)
        self.assertEqual(len(self.pressed), 1)
        self.assertFalse(self.r.tick(50.0), "scene packets stopped, so we don't know where we are")

    def test_a_new_visit_of_the_same_scene_resets_the_count(self):
        self.r.on_match_end(None)
        self.scene("CharaSelectScene", 1, 0.0)
        self.r.tick(1.0)
        self.scene("CharaSelectScene", 3, 2.0)  # left and came back
        self.assertFalse(self.r.tick(2.5))
        self.assertTrue(self.r.tick(3.0))


class EndToEndTests(unittest.TestCase):
    def _go(self, matches, **kw):
        port = free_port()
        out = {}

        def fake():
            out["side"] = fake_run("127.0.0.1", port, 0.0, "13.0.4", True, 40.0, quiet=True, matches=matches,
                                   match_seconds=1.0, menus=True, **kw)

        logs = []
        b = Bridge(port, WalkTowardPolicy(), delay=2, bot_port=2, bind="127.0.0.1", log=logs.append,
                   auto_rematch=True)
        b.rematch.settle = 0.2
        t = threading.Thread(target=fake, daemon=True)
        t.start()
        end = time.monotonic() + 40
        while time.monotonic() < end and t.is_alive():
            b.poll(0.02)
        t.join(1)
        b.close()
        return b, out["side"], logs

    def test_three_matches_unattended(self):
        b, side, logs = self._go(3)
        self.assertFalse(side.menu_presses == [], logs)
        # CSS -> match -> results -> CSS, three times; the first match starts on its own
        self.assertEqual(
            side.scenes,
            ["MeleeScene", "ResultScene", "CharaSelectScene",
             "MeleeScene", "ResultScene", "CharaSelectScene",
             "MeleeScene", "ResultScene", "CharaSelectScene"],
        )
        a = [m for s, m in side.menu_presses if s == "ResultScene"]
        plus = [m for s, m in side.menu_presses if s == "CharaSelectScene"]
        self.assertTrue(a and set(a) == {MB.A})
        self.assertTrue(plus and set(plus) == {MB.PLUS})
        self.assertEqual(b.rematch.matches_seen, 3)
        self.assertEqual(len(side.menu_presses), b.rematch.presses_total)
        # not spammed: one press per step when the game accepts it (the last
        # CSS has nothing to start, so + repeats there until the fake exits)
        self.assertEqual(len(a), 3)
        self.assertTrue(2 <= len(plus) <= 5, plus)

    def test_retries_when_the_first_press_is_ignored(self):
        b, side, logs = self._go(2, menu_ignore=1)
        self.assertEqual(side.scenes.count("MeleeScene"), 2, logs)
        a = [m for s, m in side.menu_presses if s == "ResultScene"]
        self.assertEqual(len(a), 4, "two A presses per results screen (first ignored, second accepted)")


if __name__ == "__main__":
    unittest.main()


class HardwareSceneNameTests(unittest.TestCase):
    """Scene names seen on hardware, 13.0.4."""

    def test_real_loop(self):
        from bridge.scenes import CSS, DEFAULT_NAMES, MATCH, RESULTS, STAGE
        got = [DEFAULT_NAMES.classify(n) for n in
               ("MeleeScene", "UnlockSequenceScene", "StageSelectScene", "CharaSelectScene")]
        self.assertEqual(got, [MATCH, RESULTS, STAGE, CSS])

    def test_stage_select_gets_a(self):
        from bridge import menu_buttons as MB
        from bridge.rematch import PRESS_FOR
        from bridge.scenes import STAGE
        self.assertEqual(PRESS_FOR[STAGE], MB.A)
