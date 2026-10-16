import unittest

from bridge.kinds import kind_name
from bridge.protocol import Fighter, Frame
from bridge.udp_listen import FrameTracker, SeqTracker
from bridge.wire import (
    HEADER_LEN,
    HEARTBEAT_LEN,
    KIND_HEARTBEAT,
    STATE_LEN,
    BadPacket,
    pack_header,
    pack_heartbeat,
    pack_state,
    unpack_header,
    unpack_heartbeat,
    unpack_state,
)


class WireTests(unittest.TestCase):
    def test_sizes_match_plugin(self):
        # plugin/src/net.rs: HEADER_LEN = 12, HEARTBEAT_LEN = 40
        self.assertEqual(HEADER_LEN, 12)
        self.assertEqual(HEARTBEAT_LEN, 40)

    def test_heartbeat_round_trip(self):
        hb = unpack_heartbeat(pack_heartbeat(7, 1234, "13.0.4", True, 500, 240))
        self.assertTrue(hb.hook_installed)
        self.assertEqual((hb.fighter_calls, hb.frames_queued), (500, 240))
        self.assertEqual(hb.header.kind, KIND_HEARTBEAT)
        self.assertEqual(hb.header.seq, 7)
        self.assertEqual(hb.uptime_ms, 1234)
        self.assertEqual(hb.game_version, "13.0.4")

    def test_matches_rust_byte_layout(self):
        # Built by hand the same way net.rs::heartbeat_packet does it.
        expected = (
            b"SCPU" + (4).to_bytes(2, "little") + (3).to_bytes(2, "little")
            + (5).to_bytes(4, "little") + (900).to_bytes(8, "little")
            + b"13.0.4\0\0" + (1).to_bytes(4, "little") + (77).to_bytes(4, "little")
            + (30).to_bytes(4, "little")
        )
        self.assertEqual(pack_heartbeat(5, 900, "13.0.4", True, 77, 30), expected)

    def test_rejects_bad_magic_and_version(self):
        with self.assertRaises(BadPacket):
            unpack_header(b"XXXX" + pack_header(3, 0)[4:])
        bad_ver = b"SCPU" + (99).to_bytes(2, "little") + pack_header(3, 0)[6:]
        with self.assertRaises(BadPacket):
            unpack_header(bad_ver)
        with self.assertRaises(BadPacket):
            unpack_header(b"SC")


class StateTests(unittest.TestCase):
    def _fighter(self, kind, dmg):
        return Fighter(kind, 1.5, -2.0, 0.0, 0.0, dmg, 0.0, 3, 7, 0xABCDEF0123, 4.0, -1, 0, 1)

    def test_size_matches_plugin(self):
        # plugin/src/net.rs: STATE_LEN = 12 + 8 + 2 * 64 + 4 + 4 * 8 (v4)
        self.assertEqual(STATE_LEN, 184)

    def test_state_round_trip(self):
        f = Frame(1234, 0, self._fighter(0x2D, 42.5), self._fighter(0, 10.0))
        header, back = unpack_state(pack_state(9, f))
        self.assertEqual(header.seq, 9)
        self.assertEqual(back, f)
        self.assertTrue(back.bot.airborne)
        self.assertEqual(kind_name(back.bot.fighter_kind), "robot")

    def test_heartbeat_is_not_state(self):
        with self.assertRaises(BadPacket):
            unpack_state(pack_heartbeat(1, 1, "13.0.4"))


class FrameTrackerTests(unittest.TestCase):
    def test_skips_and_new_match(self):
        t = FrameTracker()
        self.assertTrue(t.see(0))
        for f in [1, 2, 5]:
            self.assertFalse(t.see(f))
        self.assertEqual(t.skipped, 2)
        self.assertTrue(t.see(0))


class SeqTrackerTests(unittest.TestCase):
    def test_counts_gaps(self):
        t = SeqTracker()
        for s in [0, 1, 2, 5, 6]:
            t.see(s)
        self.assertEqual(t.received, 5)
        self.assertEqual(t.missing, 2)

    def test_out_of_order_and_wraparound(self):
        t = SeqTracker()
        for s in [0xFFFFFFFE, 0xFFFFFFFF, 0, 1, 0]:
            t.see(s)
        self.assertEqual(t.missing, 0)
        self.assertEqual(t.out_of_order, 1)


if __name__ == "__main__":
    unittest.main()
