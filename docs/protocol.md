# Wire protocol

See the Phase 0 design doc for the reasoning. Short version: per-frame traffic goes over UDP because only the newest packet matters, and a small TCP channel (not built yet) handles the handshake and version check. Little endian throughout.

Plugin to PC: UDP port 4243 by default (set in `sd:/ultimate/super-cpu/config.toml`).

## Header (every UDP packet)

| Field | Type | Notes |
| --- | --- | --- |
| magic | 4 bytes | `SCPU` |
| proto_version | u16 | currently 2 |
| kind | u16 | 1 = state, 2 = input, 3 = heartbeat |
| seq | u32 | per sender, wraps at 2^32 |

12 bytes. Code: `plugin/src/net.rs`, `bridge/bridge/wire.py`.

## Heartbeat (kind 3, plugin to PC) - built

| Field | Type | Notes |
| --- | --- | --- |
| header | 12 bytes | |
| uptime_ms | u64 | since the plugin loaded; going backwards means a restart |
| game_version | 8 bytes | ASCII, NUL padded, e.g. `13.0.4`. The version the game is actually running (read at boot), not the one the plugin expects |
| hook_installed | u32 | 1 once the fighter frame hook is in place |
| fighter_calls | u32 | times the hook has run (any fighter, any mode) |
| frames_queued | u32 | finished frames handed to the network thread |

40 bytes, sent every 500 ms. The last three fields (added in v2) tell you where the pipeline stops without needing plugin logs.

## Version guard (SSB-10, game version half)

At boot the plugin reads the running display version (`nn::oe::GetDisplayVersion`, same call as the Training Modpack's `src/common/events.rs`) and compares it to `EXPECTED_GAME_VERSION` in `plugin/src/version.rs`. On a mismatch it logs a `GAME VERSION MISMATCH` line, keeps sending heartbeats with the real version in `game_version`, and sends **no state packets**. The PC listener prints a warning when the heartbeat version isn't what it expects. The mod hash check isn't built yet.

## State (kind 1, plugin to PC) - built

Header, then `frame` (u32), `stage_id` (i32), then two FighterState blocks. 148 bytes, one per game frame during a match.

For now index 0 is port 1 and index 1 is port 2; bot/opponent mapping comes with SSB-6. `frame` restarts at 0 each match.

Live: everything except the invincible flag. Where each field comes from (all in `plugin/src/state.rs`, each with a Training Modpack precedent):

| Field | Read with | Precedent |
| --- | --- | --- |
| stage_id | `app::sv_information::stage_id()` | modpack `save_states.rs` (declares it), used in `tech.rs` |
| vel_x, vel_y | `KineticModule::get_sum_speed_x/_y(acc, KINETIC_ENERGY_RESERVE_ATTRIBUTE_MAIN)` | modpack `fast_fall.rs` (`get_sum_speed_y` with the same value 1) |
| shield_hp | `WorkModule::get_float(acc, FIGHTER_INSTANCE_WORK_ID_FLOAT_GUARD_SHIELD)` | `WorkModule::get_float` on instance work ids, as in modpack `charge.rs` / `debug.rs` |
| jumps_left | `JUMP_COUNT_MAX - JUMP_COUNT` via `WorkModule::get_int`, floored at 0 | `get_int` on instance work ids, as in modpack `common/mod.rs` (ENTRY_ID) |
| hitstun flag | status in `DAMAGE..DAMAGE_FALL` (end excluded) | copy of modpack `is_in_hitstun` in `common/mod.rs` |

Notes:

- Velocity is total speed in game units per frame (all kinetic energies summed, so knockback is included). Positive y is up.
- Shield is the raw shield health; a full shield is 50 in vanilla Ultimate. It's only meaningful while shielding but the game tracks it all the time, so it regens after you let go.
- The hitstun flag matches the modpack, so tumble (`DAMAGE_FALL`) is **not** counted as hitstun. If the bot needs "can't act yet" it should also look at status_kind.

### stage_id

`stage_id` is the game's `StageID` (table in `bridge/bridge/stages.py`, generated from skyline-smash `lua_const.rs`). For our v1 stages:

| Stage | stage_id | layout |
| --- | --- | --- |
| Battlefield | 0 (`BattleField`) | battlefield |
| Final Destination | 3 (`End`) | fd |
| Any stage's Omega form | the `End_*` id, e.g. 1 = `End_BattleField` | fd |
| Any stage's Battlefield form | the `Battle_*` id, e.g. 4 = `Battle_End` | battlefield |
| Big Battlefield / Small Battlefield | 2 / 347 | neither, different layouts |

Omega and Battlefield forms get their own ids, so telling an FD layout from a Battlefield layout is a lookup: `bridge.stages.stage_layout(stage_id)` returns `"fd"`, `"battlefield"` or `None`. This is the same grouping the modpack uses in `tech.rs` ("All FD Variants of Stages" / "All BF Variants of Stages"). Hardware check pending: confirm the id on the Switch for Battlefield, FD and one Omega stage (see testing-phase0.md).

### invincible flag (bit 2): not set yet

Always 0 for now. The skyline-smash bindings have `HitModule::get_total_status` / `get_status` / `get_whole` and `HIT_STATUS_*` constants, but the Training Modpack never reads hit status anywhere (it only sets it), so there's no precedented way to call these or to know which argument and return value mean "intangible or invincible right now". Guessing wrong here would quietly poison the dataset, so it stays 0 until we have a known-good reference. Status kind already covers the common cases (dodges, ledge, respawn) for a learned policy.

FighterState:

| Field | Type | Notes |
| --- | --- | --- |
| fighter_kind | i32 | |
| pos_x, pos_y | f32 | world position |
| vel_x, vel_y | f32 | |
| damage | f32 | percent |
| shield_hp | f32 | |
| stocks | i32 | |
| status_kind | i32 | |
| (padding) | 4 bytes | repr(C) alignment before the u64 |
| motion_kind | u64 | |
| anim_frame | f32 | |
| facing | i32 | -1 or 1 |
| jumps_left | i32 | jumps remaining, ground jump included |
| flags | u32 | bit 0 airborne, bit 1 hitstun, bit 2 invincible (always 0 for now) |

## Input (kind 2, PC to plugin) - not built yet

| Field | Type | Notes |
| --- | --- | --- |
| header | 12 bytes | |
| for_frame | u32 | the state frame this answers |
| apply_at | u32 | the frame it should take effect |
| stick_x, stick_y, cstick_x, cstick_y | i8 | |
| buttons | u32 | bitmask |

28 bytes.

Any layout change touches `plugin/src/net.rs` or `plugin/src/state.rs`, the matching file in `bridge/bridge/`, and this doc in the same commit, and bumps `proto_version`.
