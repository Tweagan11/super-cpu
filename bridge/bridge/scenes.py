"""Scene names and the rules for telling them apart (SSB-25).

The scene-change hook reports the game's own scene names. Only some are known
from source reading (docs/css-research.md section 4); the match and results
names are guesses until hardware experiment E1 logs a full loop. Each kind is
a list of case-insensitive substrings, so a close-enough guess still works.
Fix them here (or with run.py's --css-scene / --match-scene / --results-scene)
once E1 shows the real names.
"""
from __future__ import annotations

from dataclasses import dataclass

# What fake_switch calls each scene.
FAKE_CSS = "CharaSelectScene"
FAKE_MATCH = "MeleeScene"
FAKE_RESULTS = "ResultScene"
FAKE_STAGE = "StageSelectScene"

CSS = "css"
MATCH = "match"
RESULTS = "results"
STAGE = "stage"
OTHER = "other"


@dataclass(frozen=True)
class SceneNames:
    # Seen on hardware (13.0.4, 2026-10-16): MeleeScene -> UnlockSequenceScene
    # (results) -> StageSelectScene -> CharaSelectScene -> MeleeScene.
    css: tuple[str, ...] = ("CharaSelect",)
    results: tuple[str, ...] = ("Result", "UnlockSequence")
    match: tuple[str, ...] = ("Melee",)
    stage: tuple[str, ...] = ("StageSelect",)

    def classify(self, name: str) -> str:
        low = name.lower()
        # results first: a name like "MeleeResultScene" is results, not the match
        for kind, needles in ((RESULTS, self.results), (STAGE, self.stage), (CSS, self.css), (MATCH, self.match)):
            if any(n.lower() in low for n in needles):
                return kind
        return OTHER


DEFAULT_NAMES = SceneNames()
