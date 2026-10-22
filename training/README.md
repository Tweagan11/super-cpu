# training

| Story | File | Status |
| --- | --- | --- |
| SSB-14 Feature pipeline | `features.py` | done, live/logged parity tested |
| SSB-15 Action space | `actions.py` | done: 17 stick x 5 c-stick x 9 button combos |
| SSB-16 Behavior cloning | `train_bc.py`, `bc_policy.py` | done, verified on synthetic data only |
| SSB-26 Inverse dynamics (IDM) | `idm.py` | done, verified on synthetic data only |
| SSB-18 DAgger | `dagger.py` | not started |
| SSB-21 RL fine-tune | `rl.py` | done, verified against the fake Switch only |

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
| `rl.py` | PPO fine-tuning of a BC checkpoint, `RLPolicy` registered as `--policy rl` |
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

## DAgger with the CPU AI as the expert (SSB-18)

Plain behavior cloning drifts: the model only ever saw states a level 9 CPU
reaches, and once the bot's own mistakes put it somewhere else it has no idea
how to recover. DAgger fixes that by labeling the states the bot actually
visits with what the expert would have done there. With `drive_cpu = true` the
bot's port is a CPU card, so the level 9 AI still decides every frame; the
plugin overwrites that decision with the bot's input but also sends the
decision itself (`CAP_BOT_EXPERT`, `docs/protocol.md`). The logger stores it
in the `expert_*` columns next to the inputs the bot really played
(`docs/logging-schema.md`).

The loop:

1. Round 0: train on CPU vs CPU / human logs as usual (`python -m training.train_bc data/cpu`).
2. Play N matches with the current model: `SCPU_CHECKPOINT=checkpoints/<run>/best.pt python -m bridge.run --policy bc --bot-port 2 --log` (drive_cpu on, `docs/testing-phase0.md` 14c2 to check `rows_expert`).
3. Retrain on the original data plus those logs: `python -m training.train_bc data/cpu data/matches --dagger --bot-port 2`. Add `--expert-weight 2` to make the on-policy rows count more.
4. Repeat from 2 with the new checkpoint. Keep every round's logs (the dataset aggregates).

What `--dagger` (alias `--expert-labels`) does:

- Rows with `expert_valid = 1` use the CPU AI's decision as the target. A log that has any such rows for the bot port is a bot-played match: only its expert rows are targets (its other rows are the bot imitating itself).
- Logs without expert rows (ordinary CPU matches) keep their recorded inputs as labels.
- Features come from the state as usual, i.e. the bot's own trajectory, which is the point. The previous-input features on those rows are what the bot actually **played** (that is what it sees live), never the expert's decision.
- `--bot-port` must be the port of the bot in the bot-played logs (the CPU logs are read at the same port, so use logs where that port is the CPU you want to copy).
- `--expert-weight` (default 1.0) scales the loss of expert rows. Validation is unweighted but includes expert rows, so it measures agreement with the expert on the bot's own states.

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
- `SCPU_TEMPERATURE`: the bot samples its inputs from the model's probabilities (default 1.0). Lower is steadier, `0` is always the single most likely input, which makes the bot nearly passive: "no button, stick centered" wins most frames.
- `SCPU_HOLD`: frames to commit to each sampled input (default 2, `1` = new pick every frame). Matches the CPU's input rhythm best; every frame twitches.
- `SCPU_STICKY`: keep the previous input while its probability stays above this (default `0`, off). Large values freeze the bot on "no button".
- `SCPU_DEVICE`: `cpu` (default) or `cuda`. One `act()` (features + forward
  + decode) takes about 0.6 ms on one CPU thread, and the forward pass alone
  about 0.12 ms. The GRU variant's forward pass is about 0.85 ms on CPU.

## RL fine-tuning (SSB-21)

```
SCPU_CHECKPOINT=checkpoints/<run>/best.pt python -m bridge.run --policy rl \
    --auto-rematch --stocks 1 --log
```

1 stock, R.O.B. vs level 9 CPU, unattended. Resume with
`SCPU_RL_RESUME=checkpoints/rl/<run>` (or a `last.pt`) instead of
`SCPU_CHECKPOINT`. Needs torch. The full design is in the `rl.py` docstring;
the short version:

- Acting and learning are split. The policy samples from the current weights
  and records (features, action, logprob, value) per decision. Each match
  becomes an episode for a learner thread that runs PPO every
  `matches_per_update` matches, with its own model copy. New weights are
  swapped in at the start of the next match, never mid-match, and the frame
  loop never waits on the learner. `SCPU_RL_ASYNC_LEARN=0` updates inline.
- Reward comes from bridge frame fields only: damage dealt (+) and taken (-),
  stock taken (+5) and lost (-5), extra penalty for a probable self-destruct,
  small per-frame penalty for time offstage past a grace period. Nothing is
  counted until both fighters show stocks > 0 (hardware reads 0 at the start).
  On top: five decaying shaping terms (edgeguard KO, spike, combo extension,
  recovery, potential-based stage control), see "Reward terms" below.
- Delay: the decision on frame t applies on t + delay, so its reward is the
  one seen on that later frame (`effect_lag` shifts it further). GAE runs
  over decisions with the match end as terminal.
- Action repeat: `SCPU_RL_HOLD` (default 2) frames per decision, a fixed
  repeat like `SCPU_HOLD` in `bc_policy`, so the bot does not twitch on
  hardware. Held frames return the same input and only feed the reward
  tracker; only decision frames go to the rollout buffer, the KL penalty and
  the entropy bonus. A decision's reward is the sum of per-frame rewards over
  the frames its action was applied (`t + delay` up to the next decision's
  `t + delay`, exclusive, by frame number, so dropped frames lose nothing).
  Discount is `gamma ** hold` per decision (`gamma` keeps meaning per frame, so
  the horizon in seconds does not change with the hold; GAE `lam` is per
  decision). Stage control is a potential difference between decision frames.
  `min_decisions` counts decisions. `matches.csv` has `hold` and `decisions`.
  `SCPU_RL_HOLD=1` is the old per-frame behaviour.
- KL(pi || pi_BC) penalty (`kl_coef`), entropy bonus (`ent_coef`), value head
  trained alone for the first 2 updates, PPO clipping and a target_kl stop.
- Output: `data/rl/<run>/matches.csv` (return, win/loss, damage, stocks,
  entropy, per-term shaping totals and the decay multiplier), `updates.csv`, `config.json`. `checkpoints/rl/<run>/last.pt`
  every `ckpt_every` matches and `best.pt` by rolling win rate. Both load with
  `--policy bc` (greedy play of the tuned weights).
- Config: `SCPU_RL_<FIELD>` for every `RLConfig` field (lr, kl_coef,
  matches_per_update, learn_device, greedy, ...) and `SCPU_RL_REWARD_<FIELD>`
  for every `RewardConfig` field. `SCPU_RL_LEARN=0` or `SCPU_RL_GREEDY=1`
  plays without learning, for evaluating a checkpoint.

### Reward terms

Base reward (never decayed): damage dealt/taken (0.01 per %), stocks taken/lost
(5.0), self-destruct (-2.0 extra), offstage penalty (0.002 per frame after a 90
frame grace). On top of it come five shaping terms, each with its own weight
(`SCPU_RL_REWARD_<FIELD>`), a per-match cap on its total, and a column in
`matches.csv` (`<term>_reward` for the reward, plus a count: detections, counted
even when the cap stopped the reward). Rewards are summed per frame and credited
by applied frame exactly like the base reward (see Delay above). "Offstage"
means past the stage's ledge horizontally or `offstage_below` under the ground;
a fighter in a `CLIFF_*` status counts as on the ledge, not offstage. Hitstun is
the `in_hitstun` flag or a `DAMAGE_*` status. Units are the stream's (positions
in stage units, velocity per frame); the velocity thresholds are first guesses,
check them against logged hits.

| Term | Detects | Default weight (cap) |
| --- | --- | --- |
| edgeguard | Opponent loses a stock after at least `edgeguard_min_frames` (30) of free offstage time (offstage, not in hitstun, not on the ledge, so it was recovering). Full bonus if the bot hit it while it was offstage within `edgeguard_window` (180) frames of the KO. A fraction (`edgeguard_guard_frac` 0.5) if the bot only stood onstage within `edgeguard_near` (40) of the ledge in that window. Not paid for launches (hitstun the whole way), for dying onstage or out the top, or when the bot was nowhere near. | `edgeguard_bonus` 2.0 (6.0) |
| spike | Opponent takes damage while offstage, is in hitstun, and within `spike_window` (3) frames has vel_y <= -`spike_vel` (2.5), at least `spike_delta` (1.0) lower than just before the hit, and steeper than it is wide (or status `DAMAGE_FLY_METEOR` with vel_y < 0). Once per hit. A downward hit onstage, an upward or weak hit, or an opponent already falling that fast does not count. | `spike_bonus` 1.5 (4.5) |
| combo | Damage on the opponent while it was still in hitstun the frame before, at least `combo_min_gap` (4) frames after the previous hit, with the bot not in hitstun itself. Pays `combo_bonus` x (hits so far - 1), at most `combo_max_len` (5) times. | `combo_bonus` 0.1 (3.0) |
| recovery | Bot was offstage (off the ledge) for at least `recovery_min_frames` (30) and is now back inside the ledges or hanging on the ledge. At most once per `recovery_cooldown` (180) frames. Dying first pays nothing. | `recovery_bonus` 0.5 (2.0) |
| last_jump | Penalty: `jumps_left` goes from above 0 to 0 while the bot is offstage and more than `last_jump_far` (40) from the ledge. | `last_jump_penalty` 0.3 (1.0) |
| stage_control | Potential-based: Phi = w x (closeness(opp) - closeness(bot)), closeness = 1 - (distance inside the ledge) / `stage_control_scale` (60), clipped to [0, 1], so 1 at or beyond the ledge. Reward per frame is gamma x Phi(s') - Phi(s), and -Phi(last) at a real match end (terminal potential 0). Positions only, so by Ng et al. (1999) it cannot change the optimal policy. | `stage_control_weight` 0.05 (2.0) |

The `stage_control` cap is only a safety net: it is set wide so it never binds,
since clipping a potential term breaks the guarantee. The tracker's `gamma`
must be the RL discount; `RLPolicy` passes `gamma` from `RLConfig`, so keep them
together if you change it.

**Decay.** Terms 1-5 are multiplied (stage control through Phi) by a match-level
factor that falls linearly from 1 to `shaping_floor` (0.2) over
`shaping_decay_matches` (200) kept matches, then stays there. `0` turns the decay
off. The factor used is the `shaping_mult` column. It is fixed within a match
and follows the match count, so it continues across a resume. The base reward
is never scaled, so late in training the bot is mostly optimizing winning.

**Caps and why.** Each term's total is clamped to its cap per match (after the
decay multiplier). A cap means a policy that finds a way to farm one term gets a
bounded payout, so it cannot out-earn actually winning (a stock is worth 5.0).
Caps count after decay, so they shrink with the multiplier only through the
rewards, not by themselves.

**Tuning.** Look at the per-term columns in `matches.csv` next to `win`. Healthy:
`shaping_total` is a small fraction of `return` and wins go up with it. If a
term's total sits at its cap in most matches, either lower its weight or the
bot found a loop (below). To switch a term off set its weight to 0
(`SCPU_RL_REWARD_SPIKE_BONUS=0`); set all five and the run is the base reward.

**What reward hacking looks like**

- edgeguard: the bot keeps the opponent offstage-ish without killing, or stands
  at the ledge waiting. The count rises while `stocks_taken` does not; the
  half-pay for just standing near the ledge is the weak spot, lower
  `edgeguard_guard_frac` if so. Also, hits from a stage hazard are credited to the bot.
- spike: pays on the opponent's hit, not on a KO. A bot that chases weak downward
  hits at the ledge (`spikes` high, low `damage_dealt` and no KOs) is farming it.
  Raise `spike_vel` / `spike_delta`, or lower the weight. Fast-falling and
  character-specific knockback make this the term to verify on real logs.
- combo: spamming multi-hit or rapid moves into a stunned opponent (`combo_hits`
  in the tens, little damage). The gap, the length multiplier cap and the total
  cap limit it; the cap should bind before a real 1-stock win does.
- recovery: dropping off the ledge on purpose and grabbing it again (`recoveries`
  up, `offstage_frames` up, stocks lost up). The minimum excursion, the cooldown
  and the cap limit it. `last_jump_penalty` can also make the bot hold its jump
  and die with it unused: if `stocks_lost` rises while `last_jumps` falls, lower it.
- stage_control: cannot change the optimal policy by construction, but it does
  speed up or distort learning when the weight is large relative to the base.
  Standing at center and not fighting shows as a rising `stage_control_reward`
  with low `damage_dealt`; lower the weight.

## Curriculum (SSB-30)

RL learns fastest against an opponent it beats some of the time. Game
settings, nothing to build:

- One opponent character on the CPU card (not Random), Final Destination.
- Start at CPU level 7 and pass the level to the bridge: `--opponent cpu7`.
- Step up one level when the log says so: past 50% wins over the last 40
  matches against that opponent, `[rl] curriculum: ... Time to raise the CPU
  level` is printed once. Change the level on the CPU card and restart with
  the new `--opponent` (keep `SCPU_RL_RESUME`).
- Add a second opponent character only after reaching level 9 with the first.

Every 20 matches the log prints the wins in the last 40 per (opponent,
fighter). `matches.csv` has `opponent` and `opp_fighter` columns.

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
