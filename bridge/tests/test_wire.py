import unittest

from bridge.udp_listen import SeqTracker
from bridge.wire import (
    HEADER_LEN,
    HEARTBEAT_LEN,
    KIND_HEARTBEAT,
    BadPacket,
    pack_header,
    pack_heartbeat,
    unpack_header,
    unpack_heartbeat,
)


class WireTests(unittest.TestCase):
    def test_sizes_match_plugin(self):
        # plugin/src/net.rs: HEADER_LEN = 12, HEARTBEAT_LEN = 28
        self.assertEqual(HEADER_LEN, 12)
        self.assertEqual(HEARTBEAT_LEN, 28)

    def test_heartbeat_round_trip(self):
        hb = unpack_heartbeat(pack_heartbeat(7, 1234, "13.0.4"))
        self.assertEqual(hb.header.kind, KIND_HEARTBEAT)
        self.assertEqual(hb.header.seq, 7)
        self.assertEqual(hb.uptime_ms, 1234)
        self.assertEqual(hb.game_version, "13.0.4")

    def test_matches_rust_byte_layout(self):
        # Built by hand the same way net.rs::heartbeat_packet does it.
        expected = (
            b"SCPU" + (1).to_bytes(2, "little") + (3).to_bytes(2, "little")
            + (5).to_bytes(4, "little") + (900).to_bytes(8, "little")
            + b"13.0.4\0\0"
        )
        self.assertEqual(pack_heartbeat(5, 900, "13.0.4"), expected)

    def test_rejects_bad_magic_and_version(self):
        with self.assertRaises(BadPacket):
            unpack_header(b"XXXX" + pack_header(3, 0)[4:])
        bad_ver = b"SCPU" + (99).to_bytes(2, "little") + pack_header(3, 0)[6:]
        with self.assertRaises(BadPacket):
            unpack_header(bad_ver)
        with self.assertRaises(BadPacket):
            unpack_header(b"SC")


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
