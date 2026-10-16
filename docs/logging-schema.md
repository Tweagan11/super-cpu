# Logging schema (SSB-9)

One file per match, one record per frame per player, plus a JSON header.
Code: `bridge/bridge/logger.py`. Match boundaries: `bridge/bridge/matches.py`.

## Files

Matches land in `data/matches/` (git-ignored):

```
data/matches/<match_id>.npz    the match: header + frame arrays (numpy, compressed)
data/matches/<match_id>.json   copy of the header, for grepping without numpy
```

`match_id` is `YYYYMMDD-HHMMSS-<6 hex>`, UTC start time plus a random suffix.

Write one with `python -m bridge.udp_listen --log` (human or CPU play), `python -m bridge.run --log` (the bot playing), or through the eval harness. Read one with:

```python
from bridge.logger import load_match
m = load_match("data/matches/20261015-201500-a1b2c3.npz", expect_version="13.0.4")
m.header["winner_port"]
m.frames["frame"]            # shape (n,)
m.port(2)["damage"]          # port 2 damage every frame, shape (n,)
m.players["pos_x"][:, 0]     # same thing, port 1 x
```

`python -m bridge.inspect_log <file>...` prints a summary.

The `.npz` holds three arrays: `header` (the JSON as a 0-d string), `frames`, and `players`. Load with `allow_pickle=False`; nothing in it needs pickle.

## Match header

| Key | Notes |
| --- | --- |
| `schema_version` | 1. Bump on any change to the arrays or header meaning |
| `match_id` | see above |
| `started_at`, `ended_at` | ISO 8601, UTC, PC clock |
| `game_version` | from the plugin heartbeat, e.g. `13.0.4` |
| `mod_hash` | from `--mod-hash`. `unset` until SSB-10 freezes the mod list |
| `stage_id` | from the state stream (`app::sv_information::stage_id()`). 0 is Battlefield. -1 means unknown |
| `stage_layout` | `battlefield` or `fd` (Omega forms count as `fd`) |
| `stage_source` | `stage_id` when the id maps to Battlefield or FD/Omega, else `flag` (from `--stage`) |
| `ruleset` | `{"stocks": 3, "items": false, "time_limit_min": null}` for v1 |
| `ports` | list of 2, see below |
| `inputs_present` | true if any row of either port has `input_valid = 1` |
| `input_capture` | where the inputs came from, see below. Added without a schema bump (new key, old keys unchanged) |
| `frame_count` | rows in the file |
| `first_frame`, `last_frame` | game frame counter at each end |
| `frames_skipped` | gaps in the frame counter (dropped packets) |
| `duration_s` | PC time from first to last frame |
| `end_reason` | `state_stopped` (normal: state stopped and a fighter is out of stocks, results screen), `frame_reset` (next match started with no gap, also a match quit from the pause menu), `stopped_long` (state stopped for 5 min with stocks left: paused and walked away, or a timed match), `interrupted` (PC side shut down, match cut off). State also stops while paused; with stocks left the match stays open and continues when it resumes |
| `complete` | false only for `interrupted` |
| `winner_port` | 1, 2, or null |
| `winner_source` | `stocks`, or `dead_status` if stocks tie and one fighter is in DEAD on the last frame (the final stock can drop after the plugin stops sending) |

Each `ports` entry: `slot` (1 or 2), `fighter_kind`, `fighter_name`, `controller` (human name, `bot:<model>`, or `cpu:9`), `start_stocks`, `final_stocks`, `final_damage`, `stocks_lost`, `damage_taken` (sum of damage increases, so it counts across stocks), `inputs_present`.

## Per-frame records

`frames`, shape `(n,)`, one row per received state packet:

| Field | Type | Notes |
| --- | --- | --- |
| frame | u32 | game frame counter |
| seq | u32 | UDP packet seq |
| recv_t | f64 | seconds since the first frame of the match, PC clock |
| stage_id | i32 | |

`players`, shape `(n, 2)`, column 0 is port 1:

| Field | Type | Notes |
| --- | --- | --- |
| fighter_kind, pos_x, pos_y, vel_x, vel_y, damage, shield_hp, stocks, status_kind, motion_kind, anim_frame, facing, jumps_left, flags | same as the state message | see `protocol.md`. Fields the plugin sends as 0 are logged as 0 |
| stick_x, stick_y, cstick_x, cstick_y | i8 | inputs that frame |
| buttons | u32 | `bridge/buttons.py` bits |
| input_valid | u8 | 1 if this row's inputs were recorded, 0 if zero-filled |

Inputs (SSB-13): each row's input columns are the final mapped inputs that port's fighter read **on that frame**, with `input_valid = 1`. Rows with `input_valid = 0` are zero-filled and mean "unknown", not "neutral". Three sources, in this order (a later one only fills rows the earlier ones left invalid):

1. **The bot's acks** (`bridge.run --log` only). `bridge.run` remembers what it sent for each state frame; when the plugin acks `(for_frame, applied_frame)`, `MatchLogger.record_inputs(bot_port, applied_frame, inputs)` puts it on the frame it took effect, not the frame it was decided on. Repeats and neutral frames aren't acked, so on their own they stay invalid.
2. **The plugin's capture** (state packet v3, `docs/protocol.md`). Both ports, every frame, including what the bot really got on repeat and neutral frames. This wins when both exist.

3. **The control module** (state packet v4). The fighter's own read of its controller, translated to the buttons.py bits (`docs/protocol.md`). This is the only source for CPU ports, which the final input mapping never sees. It never overrides 1 or 2; where both exist the two are compared (below).

So with a v4 plugin both ports are filled almost every frame, human, bot or CPU, and the acks and the control module are cross-checks. The human FIM capture stays the label source for human ports until the control module translation is validated on hardware.

`input_capture` header:

| Key | Notes |
| --- | --- |
| `mapping.source` | `control_status` (from the plugin) or `none` (no control status seen, mapping unknown) |
| `mapping.bot_port` | the plugin's `bot_port` |
| `mapping.player_idx_by_port` | final input mapping `player_idx` logged as `[port 1, port 2]` |
| `mapping.fim_override` | `fim_player_idx` was set in config.toml |
| `mapping.verified` | true for the default mapping (player_idx = port - 1, confirmed on hardware 2026-10-16), false with a `fim_player_idx` override or no control status. Logs made before this change say false but used the default mapping, so they are fine |
| `rows_captured` | rows per port filled from the plugin's capture |
| `rows_from_acks` | rows per port filled from acks |
| `ack_capture_mismatches` | rows where an acked input and the captured one disagree. Should be 0; anything else means the frame alignment is off |
| `port_sources` | per port, which source(s) filled the rows: `fim`, `ack`, `control_module`, joined with `+` when several (`ack+fim` for the bot), `none` if the port has no inputs. A CPU port in a CPU vs CPU match is `control_module` |
| `rows_control_module` | rows per port where the plugin sent a control module read (v4) |
| `rows_from_control_module` | rows per port whose `input_valid` exists only because of it (no ack, no capture) |
| `control_module_agreement` | per port, rows where both the FIM capture and the control module exist, compared after encoding each to `training/actions.py` classes: `rows_compared`, `main`, `cstick`, `buttons` (rows in the same class on that head), `all` (all three heads agree) and `all_pct`. null values mean `training` couldn't be imported; `all_pct` is null when nothing was compared. CPU ports always have `rows_compared` 0. For a human port `all_pct` near 100 means the translation is right; a low `buttons` number points at a wrong button mapping, a low `main`/`cstick` at stick scaling or a swapped axis |

`frame_at()` rebuilds `Frame.inputs` from valid rows (None elsewhere).

Rows are only frames that arrived. Dropped packets leave gaps in `frame`; nothing is interpolated.

## Labeled copies (SSB-26)

`python -m training.idm label` writes `<stem>.inferred.npz` next to a log, never over it. It adds a bool array `inferred` `(n, 2)` to the npz (`LoadedMatch.inferred`, None for ordinary logs) and header keys `derived_from` and `inferred_inputs` (per port: `model`, `rows`, `unlabeled_rows`, `future`, `window`, `min_conf`, `mean_conf`). On marked rows the input columns are predictions and `input_valid` stays 0. Training ignores them unless asked (`--include-inferred`).

## Rules

- A match whose heartbeat `game_version` isn't the expected one (`--expect-version`, default 13.0.4), or that changed mid-match, or that had no heartbeat at all, is not written. The logger prints why.
- `load_match(path, expect_version=..., expect_mod_hash=...)` raises `LogRejected` on a mismatch. Training code should always pass both.
- Raw logs are kept as-is forever. Features live in a separate derived step.
- Data lives in `data/`, which is git-ignored.
