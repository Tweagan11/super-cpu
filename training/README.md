# training

| Story | File | Status |
| --- | --- | --- |
| SSB-14 Feature pipeline | `features.py` | done, live/logged parity tested |
| SSB-15 Action space | `actions.py` | done: 17 stick x 5 c-stick x 9 button combos |
| SSB-16 Behavior cloning | `train_bc.py`, `bc_policy.py` | done, verified on synthetic data only |
| SSB-26 Inverse dynamics (IDM) | `idm.py` | done, verified on synthetic data only |
| SSB-18 DAgger | `dagger.py` | not started |
| SSB-21 RL fine-tune | `rl.py` | not started |

Rules that keep data usable:

- Raw logs are never edited. Features are always derived.
- The feature code used in training is the same code the bridge runs live.
- Train with input delay randomized from 0 to the worst case measured in SSB-7.

## Setup

There's no separate training package. Install the bridge with the `train`
extra (adds torch) and run everything from the repo root with `python -m training.<thing>`.
`training/__init__.py` puts `bridge/` on the path, so the in-repo bridge is
always the one used.

```
pip install -e "bridge[train]"
```

On the 2080 Super, install the CUDA build of torch first (see pytorch.org for
the right `--index-url`), then the line above.

## Files

| File | What it does |
| --- | --- |
| `features.py` | logged matches or a live `Sequence[Frame]` -> feature vectors. One core (`window_features`) for both |
| `actions.py` | action classes, encode/decode, `python -m training.actions <files>` round trip report |
| `train_bc.py` | the trainer, model (`BCNet`) and checkpoint load/save |
| `bc_policy.py` | `BCPolicy`, registered as `--policy bc` in `bridge/bridge/policy.py` |
| `idm.py` | inverse dynamics model: `train`, `eval`, `label` (reuses `BCNet`, `window_features`) |
| `synthetic.py` | fake logged matches with known labels, for tests before real data exists |
| `smoke_test.py` | the whole pipeline end to end on synthetic data |
| `tests/` | unit tests |

## Features (SSB-14)

The full description is the docstring at the top of `features.py`. Short version, defaults (`--window 12 --prev-inputs 4`):

- 12 game frames of history, by frame number. 35 floats per frame: 15 per
  fighter (pos relative to stage center, vel, damage, shield, stocks,
  anim_frame, facing, jumps, airborne/hitstun/invincible, distance to ledge,
  offstage) for the bot then the opponent, plus dx, dy, distance, a pad flag
  (before match start) and an age (frames since this slot's data was
  actually received, for dropped frames).
- plus delay / 8. Total 421 floats.
- 39 categoricals for embeddings: bot and opponent status_kind for each of
  the 12 frames, both fighter kinds, stage layout (unknown / battlefield /
  fd), and the bot's own last 4 inputs as action classes (main, c-stick,
  buttons).
- Bot perspective by `--bot-port`. Not mirrored (reasons in the docstring).
- Dropped frames forward-fill with `age` set. Rows with `input_valid == 0`
  are never labels but still count as history.
- Paths are loaded with `expect_version="13.0.4"`, so other builds raise `LogRejected`.

`tests/test_features.py` checks the dataset path and the live path give
bit for bit identical features for the same frames, including match start
and right after dropped frames, for windows 8, 12 and 16 and both ports.

## Action space (SSB-15)

Three heads: main stick 17 (center, 8 directions at full and half tilt),
c-stick 5 (center, 4 cardinals), buttons 9: none, attack, special, jump,
jump_mini, guard, catch, guard+jump, attack+jump. Why those (and what gets
folded or dropped) is in the `actions.py` docstring.

Once real inputs are logged, check how lossy it is:

```
python -m training.actions data/matches/*.npz --port 1
```

If a combo shows up a lot under "combos not in the table", add it to
`BUTTON_COMBOS`. That changes the model's output size, so retrain (old
checkpoints refuse to load).

## Training (SSB-16)

```
python -m training.train_bc data/matches --device cuda --epochs 30
python -m training.train_bc data/matches --device cpu --bot-port 2 --max-delay 6
python -m training.train_bc data/matches --model gru     # optional GRU variant
```

- MLP over the flattened window: 2 x 256 hidden, ReLU, dropout 0.1, one
  linear head per action head, summed cross-entropy. 271k params.
- Delay: each epoch every sample gets a fresh d from 0..`--max-delay` (default
  4) among the delays that have a label. Features see frames up to t, the
  label is the bot's input on frame t + d, d / 8 is a feature, and the "own
  last inputs" are the ones for frames t+d-4 .. t+d-1. Full details in the
  `train_bc.py` docstring.
- `--prev-dropout 0.5` (default): half the samples have their own past inputs
  hidden. Without it the model learns "do what I did last frame", which
  scores great on val but falls apart live, where its past
  inputs are its own guesses. On synthetic data this was the difference
  between baseline-level live accuracy and 93%.
- Train/val split by match (`--val-frac 0.2`, `--seed`). Val uses a fixed
  delay per sample so epochs are comparable. Per-head val accuracy and a
  majority-class baseline are printed each epoch.
- Checkpoints go to `checkpoints/<timestamp>-<model>-s<seed>/{best,last}.pt`
  plus `summary.json`. `checkpoints/` and `data/` are git-ignored.

## Inverse dynamics model (SSB-26)

A BC policy sees state up to frame t and guesses the input. An IDM sees state
around t, past and future (default `t-7 .. t+8`), and says which input was
pressed on t. That is much easier, so it can label matches that have no inputs:
a Smash replay streamed through the plugin, or a port with `input_valid = 0`.
The labeled rows then become BC data, which is the cheap way to get a lot more
of it than we can play.

```
python -m training.idm train data/matches --device cuda --out checkpoints/idm1
python -m training.idm eval  checkpoints/idm1/best.pt data/matches            # the checkpoint's held-out matches
python -m training.idm label checkpoints/idm1/best.pt data/replays --port 2   # writes <stem>.inferred.npz
python -m training.train_bc data/matches data/replays --include-inferred --bot-port 2
```

- Same features (`features.window_features`), same action classes, same
  `BCNet`. A sample is (row, port): perspective is that port, and both ports
  with recorded inputs are used. No past-input features. Needs frame t+8 in the
  log, so the last frames and rows next to a dropped packet at t+8 stay unlabeled.
- `eval` prints per-head accuracy next to the majority baseline, exact action
  accuracy, top confusions per head and button classes by recall (HARD < 50%).
  Always judge against the baseline: the c-stick is centered most of the time.
- `label` never edits its input. The new file has the same frames and an
  `inferred` bool array (n, 2). On those rows the input columns hold the
  prediction but `input_valid` stays 0, so tools that don't know about IDMs
  still treat them as unknown. Header: `derived_from` (raw match id) and
  `inferred_inputs: {"<port>": {"model", "rows", "unlabeled_rows", "mean_conf", ...}}`.
  `--min-conf` leaves low-confidence rows unlabeled.
- `build_dataset` and `train_bc` ignore inferred rows unless
  `--include-inferred` / `include_inferred=True`. If a raw log and its labeled
  copy are both given, only one is used (the copy only with the flag), so a
  directory holding both is not double counted. The IDM itself never trains
  on inferred rows.
- Not checked on real data yet. Run `eval` on held-out human matches first;
  label noise is what BC inherits, and the hard classes it prints (likely
  short hop vs jump, half vs full tilt) are the ones to distrust.

Plan for replays: (1) hardware question, does the plugin stream state while a
replay plays back? (`docs/testing-phase0.md` section 12). (2) If yes, stream
replays with `udp_listen --log` (no inputs, `input_valid = 0` on both
ports). (3) Train the IDM on matches with captured inputs, `label` the replays
for both ports, train BC with `--include-inferred`. (4) Compare the 50-game
eval against the recorded-only model before trusting it.

## Running it as the bot

```
SCPU_CHECKPOINT=checkpoints/<run>/best.pt SCPU_DELAY=3 python -m bridge.run --policy bc
```

Checked against the fake Switch: a smoke-test checkpoint drives the bot
through `bridge.run` with 0 late inputs at `--delay 2`.

- `SCPU_DELAY`: the input delay measured in SSB-7. Defaults to the
  checkpoint's `--max-delay`.
- `SCPU_BOT_PORT`: override the port the checkpoint was trained for.
- `SCPU_DEVICE`: `cpu` (default) or `cuda`. One `act()` (features + forward
  + decode) takes about 0.6 ms on one CPU thread, and the forward pass alone
  about 0.12 ms. The GRU variant's forward pass is about 0.85 ms on CPU.

## Tests

From the repo root:

```
python -m unittest discover -s training/tests -t .    # ~5 s, trainer tests skip without torch
python -m training.smoke_test                          # ~25 s on CPU, end to end
cd bridge && python -m unittest discover -s tests -t . # bridge tests
```

The smoke test writes 12 synthetic matches through the real logger, trains
for 20 epochs on CPU, loads the checkpoint through `POLICIES["bc"]`, and
plays 4 unseen matches frame by frame, comparing the bot to the rule at
delays 0, 2 and 4. `--rule scripted --bot-port 2` clones the scripted
baseline instead.

## Training on real data, once human inputs are logged

1. Make sure the logs have `inputs_present: true` for the human's port
   (`python -m bridge.inspect_log data/matches/*.npz`).
2. Run `python -m training.actions data/matches/*.npz --port N` and look at
   the round trip numbers. Adjust `BUTTON_COMBOS` if something common is missing.
3. Train: `python -m training.train_bc data/matches --device cuda --bot-port N --max-delay <SSB-7 worst case>`.
   Pass `--expect-mod-hash` once SSB-10 freezes the mod list.
4. Compare val accuracy to the printed majority baseline (main stick sits at
   "center" a lot, so raw accuracy flatters).
5. Play it: `SCPU_CHECKPOINT=... SCPU_DELAY=... python -m bridge.run --policy bc`, then the 50-game eval.
