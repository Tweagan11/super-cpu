import unittest

from bridge.matches import MatchWatcher
from bridge.wire import LogDedup, LogLine, pack_log, unpack_log


class LogPacketTests(unittest.TestCase):
    def test_matches_rust_layout(self):
        # plugin/src/log.rs packet_layout test: seq 7, id 3, "hi"
        b = pack_log(7, 3, "hi")
        self.assertEqual(b.hex(), "53435055" "0300" "0600" "07000000" "03000000" "0200" "6869")
        line = unpack_log(b)
        self.assertEqual((line.id, line.text), (3, "hi"))

    def test_watcher_returns_log_lines(self):
        _, msg = MatchWatcher().packet(pack_log(1, 0, "inputs on"), 0.0)
        self.assertIsInstance(msg, LogLine)

    def test_dedup_and_restart(self):
        d = LogDedup()
        lines = [unpack_log(pack_log(0, i, "x")) for i in (0, 1, 0, 1, 2)]
        self.assertEqual([d.new(x) for x in lines], [True, True, False, False, True])
        d.reset()  # plugin restarted
        self.assertTrue(d.new(lines[0]))


if __name__ == "__main__":
    unittest.main()
