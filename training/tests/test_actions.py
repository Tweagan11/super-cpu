import itertools
import tempfile
import unittest

import numpy as np

import training  # noqa: F401  (puts bridge/ on the path)
from bridge import buttons as B
from bridge.logger import load_match
from bridge.protocol import Inputs
from training import actions as A
from training.synthetic import write_synthetic_matches


class ActionTests(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual((A.N_MAIN, A.N_CSTICK, A.N_BUTTONS), (17, 5, 9))
        self.assertEqual(len(A.BUTTON_NAMES), A.N_BUTTONS)
        self.assertEqual(len(set(A.BUTTON_COMBOS)), A.N_BUTTONS)

    def test_round_trip_every_class(self):
        for m, c, b in itertools.product(range(A.N_MAIN), range(A.N_CSTICK), range(A.N_BUTTONS)):
            got = A.encode(A.decode(m, c, b))
            self.assertEqual((got.main, got.cstick, got.buttons), (m, c, b))

    def test_encode_known_inputs(self):
        self.assertEqual(A.encode(Inputs()), A.ActionClasses(0, 0, 0))
        self.assertEqual(A.encode(Inputs(stick_x=120, stick_y=3)).main, 1)          # full right
        self.assertEqual(A.MAIN_STICK[A.encode(Inputs(stick_x=-60)).main], (-64, 0))  # half left
        self.assertEqual(A.C_STICK[A.encode(Inputs(cstick_y=-100)).cstick], (0, -127))
        self.assertEqual(A.encode(Inputs(buttons=B.ATTACK | B.JUMP)).buttons, 8)

    def test_button_folding(self):
        self.assertEqual(A.button_class(B.ATTACK_RAW), A.button_class(B.ATTACK))
        self.assertEqual(A.button_class(B.GUARD_HOLD), A.button_class(B.GUARD))
        self.assertEqual(A.button_class(B.SPECIAL_RAW2 | B.CSTICK_ON), A.button_class(B.SPECIAL))
        self.assertEqual(A.button_class(B.GUARD | B.ATTACK), A.button_class(B.CATCH))  # shield grab
        self.assertEqual(A.button_class(B.APPEAL_HI), 0)  # taunts dropped
        # not a combo: keep the most important bit
        self.assertEqual(A.BUTTON_COMBOS[A.button_class(B.SPECIAL | B.JUMP)], B.SPECIAL)

    def test_vectorized_matches_scalar(self):
        rng = np.random.default_rng(0)
        n = 3000
        sx, sy, cx, cy = (rng.integers(-128, 128, n) for _ in range(4))
        btn = rng.integers(0, 1 << 18, n)
        btn[:500] = rng.choice(A.BUTTON_COMBOS, 500)
        m, c, b = A.encode_arrays(sx, sy, cx, cy, btn)
        for i in range(0, n, 7):
            e = A.encode(Inputs(int(sx[i]), int(sy[i]), int(cx[i]), int(cy[i]), int(btn[i])))
            self.assertEqual((e.main, e.cstick, e.buttons), (m[i], c[i], b[i]))
        dec = A.decode_arrays(m, c, b)
        d0 = A.decode(int(m[3]), int(c[3]), int(b[3]))
        self.assertEqual(tuple(int(x[3]) for x in dec), (d0.stick_x, d0.stick_y, d0.cstick_x, d0.cstick_y, d0.buttons))

    def test_report(self):
        rep = A.RoundTripReport()
        rep.add([127, 50, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0], [B.ATTACK, B.ATTACK_RAW, B.APPEAL_HI])
        self.assertEqual(rep.rows, 3)
        self.assertEqual(rep.main_exact, 2)        # 50 snaps to 64
        self.assertEqual(rep.buttons_exact, 1)     # ATTACK_RAW decodes as ATTACK
        self.assertEqual(rep.buttons_canonical, 3)  # but all match on kept bits
        self.assertEqual(rep.dropped_bits[B.APPEAL_HI], 1)
        self.assertIn("exact round trip", rep.text())

    def test_report_on_logged_matches(self):
        with tempfile.TemporaryDirectory() as d:
            paths = write_synthetic_matches(d, n_matches=2, n_frames=200, seed=3)
            rep = A.roundtrip_report([load_match(p, expect_version="13.0.4") for p in paths], port=1)
            self.assertGreater(rep.rows, 300)
            self.assertEqual(rep.exact, rep.rows)  # the synthetic rule only uses table values


if __name__ == "__main__":
    unittest.main()
