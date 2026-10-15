import csv
import tempfile
import unittest
from pathlib import Path

from bridge.eval import MATCH_COLUMNS, RESULTS_COLUMNS, EvalSession, aggregate, ensure_header
from bridge.fake_switch import scripted_match
from bridge.matches import MatchWatcher

from .test_logger import play


def read(path):
    with Path(path).open(newline="") as f:
        return list(csv.DictReader(f))


class EvalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.results = self.dir / "results.csv"
        # Start from the repo's header as it was before the stage column
        self.results.write_text(
            "date,model,game_version,mod_hash,platform,opponent,games,wins,stocks_taken,avg_damage_dealt\n"
            "2026-10-01,old,13.0.4,unset,switch,cpu9,50,10,40,55.0\n"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def session(self, **kw):
        kw.setdefault("echo", lambda s: None)
        return EvalSession(model="scripted", results_csv=self.results, data_dir=self.dir / "data",
                           min_seconds=0.5, **kw)

    def test_three_games_one_row(self):
        s = self.session(games=3, bot_port=2, stage="battlefield")
        w = MatchWatcher(s.observers())
        # P2 wins (lost 0), P2 wins (lost 1), P1 wins (P2 loses 3, takes 2)
        for i, (winner, wl) in enumerate([(2, 0), (2, 1), (1, 2)]):
            play(w, scripted_match(120, winner, wl), t0=10.0 * i)
        self.assertTrue(s.done)

        rows = read(self.results)
        self.assertEqual(len(rows), 2)
        self.assertEqual(list(rows[0]), RESULTS_COLUMNS)  # header upgraded, old row kept
        self.assertEqual(rows[0]["model"], "old")
        self.assertEqual(rows[0]["stage"], "")
        r = rows[1]
        self.assertEqual((r["games"], r["wins"], r["stocks_taken"]), ("3", "2", "8"))
        self.assertEqual(r["stage"], "battlefield")
        self.assertEqual(r["game_version"], "13.0.4")
        self.assertEqual(r["opponent"], "cpu9")

        detail = read(s.match_csv)
        self.assertEqual(list(detail[0]), MATCH_COLUMNS)
        self.assertEqual([d["win"] for d in detail], ["1", "1", "0"])
        self.assertEqual([d["stocks_lost"] for d in detail], ["0", "1", "3"])
        self.assertTrue(all(Path(d["log_path"]).exists() for d in detail))
        self.assertEqual(len(list((self.dir / "data" / "matches").glob("*.npz"))), 3)

    def test_rejected_and_short_matches_dont_count(self):
        s = self.session(games=1, bot_port=1)
        w = MatchWatcher(s.observers())
        play(w, scripted_match(120, 1, 0), t0=0.0, version="13.0.3")   # wrong version
        play(w, scripted_match(10, 1, 0), t0=10.0)                     # too short
        self.assertEqual(s.skipped, 2)
        self.assertFalse(s.done)
        self.assertEqual(len(read(self.results)), 1)  # no new row yet
        play(w, scripted_match(120, 1, 0), t0=20.0)
        self.assertTrue(s.done)
        self.assertEqual(read(self.results)[-1]["wins"], "1")

    def test_extra_matches_after_done_are_ignored(self):
        s = self.session(games=1)
        w = MatchWatcher(s.observers())
        play(w, scripted_match(120, 2, 0), t0=0.0)
        play(w, scripted_match(120, 2, 0), t0=10.0)
        self.assertEqual(len(s.rows), 1)
        self.assertEqual(len(read(self.results)), 2)

    def test_no_log_mode(self):
        s = self.session(games=1, write_logs=False)
        play(MatchWatcher(s.observers()), scripted_match(120, 2, 0))
        self.assertTrue(s.done)
        self.assertFalse((self.dir / "data" / "matches").exists())
        self.assertEqual(s.rows[0]["log_path"], "")

    def test_aggregate(self):
        rows = [
            {"stage": "fd", "game_version": "13.0.4", "win": 1, "stocks_taken": 3, "damage_dealt": 100.0},
            {"stage": "battlefield", "game_version": "13.0.4", "win": 0, "stocks_taken": 1, "damage_dealt": 50.0},
        ]
        a = aggregate(rows, date="2026-10-15", model="m", mod_hash="x", platform="switch", opponent="cpu9")
        self.assertEqual((a["games"], a["wins"], a["stocks_taken"], a["avg_damage_dealt"]), (2, 1, 4, 75.0))
        self.assertEqual(a["stage"], "mixed")
        self.assertEqual(list(a), RESULTS_COLUMNS)

    def test_ensure_header_refuses_unknown_columns(self):
        p = self.dir / "weird.csv"
        p.write_text("date,surprise\n")
        with self.assertRaises(ValueError):
            ensure_header(p, RESULTS_COLUMNS)


if __name__ == "__main__":
    unittest.main()
