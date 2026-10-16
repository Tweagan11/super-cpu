import unittest
from dataclasses import replace

from bridge.matches import MatchStarted
from bridge.measure import StageMeasurer
from bridge.stages import STAGE_ID_BATTLEFIELD
from bridge.statuses import STATUS_NAMES
from tests.helpers import fighter, frame

CLIFF = next(k for k, v in STATUS_NAMES.items() if v.startswith("CLIFF"))
DEAD = next(k for k, v in STATUS_NAMES.items() if v == "DEAD")


class MeasureTests(unittest.TestCase):
    def test_ground_platforms_and_layout_code(self):
        out: list[str] = []
        m = StageMeasurer(echo=out.append)
        m.on_match_start(MatchStarted(frame(0, stage_id=STAGE_ID_BATTLEFIELD), 0.0))
        n = 0

        def stand(x, y, count=12, **kw):
            nonlocal n
            for _ in range(count):
                f = replace(fighter(), pos_x=x, pos_y=y, flags=0, **kw)
                m.on_frame(frame(n, bot=f, opp=replace(f, status_kind=DEAD), stage_id=STAGE_ID_BATTLEFIELD), n, 0.0)
                n += 1

        stand(-70.0, 0.0)
        stand(70.0, 0.0)
        stand(-50.0, 27.0)
        stand(-22.0, 27.0)
        stand(0.0, 54.0)
        stand(90.0, -5.0, status_kind=CLIFF)  # hanging on ledge: ignored
        stand(10.0, 80.0, count=3)              # too few frames: ignored

        text = m.report()
        self.assertIn("main stage: x -70.0 to 70.0, ground y 0.0", text)
        self.assertIn("platform:   x -50.0 to -22.0, y 27.0", text)
        self.assertIn("platform:   x 0.0 to 0.0, y 54.0", text)
        self.assertIn("ledge_x=70.0", text)
        self.assertIn("name='battlefield'", text)
        self.assertNotIn("90.0", text)
        self.assertNotIn("80.0", text)

    def test_nothing_measured(self):
        out: list[str] = []
        m = StageMeasurer(echo=out.append)
        self.assertIsNone(m.report())
        self.assertIn("no fighter frames", out[0])


class TrainingModeTests(unittest.TestCase):
    def test_zero_stocks_still_measured(self):
        """Training mode and timed matches report 0 stocks; those frames still count."""
        out: list[str] = []
        m = StageMeasurer(echo=out.append)
        m.on_match_start(MatchStarted(frame(0), 0.0))
        for n, x in enumerate([-80.0] * 12 + [80.0] * 12):
            f = replace(fighter(), pos_x=x, pos_y=0.0, flags=0, stocks=0)
            m.on_frame(frame(n, bot=f, opp=f), n, 0.0)
        self.assertIsNotNone(m.report())

    def test_explains_skips(self):
        out: list[str] = []
        m = StageMeasurer(echo=out.append)
        m.on_match_start(MatchStarted(frame(0), 0.0))
        self.assertIsNone(m.report())
        self.assertIn("no fighter frames", out[-1])


if __name__ == "__main__":
    unittest.main()
