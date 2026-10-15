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
| game_version | 8 bytes | ASCII, NUL padded, e.g. `13.0.4` |
| hook_installed | u32 | 1 once the fighter frame hook is in place |
| fighter_calls | u32 | times the hook has run (any fighter, any mode) |
| frames_queued | u32 | finished frames handed to the network thread |

40 bytes, sent every 500 ms. The last three fields (added in v2) tell you where the pipeline stops without needing plugin logs.

## State (kind 1, plugin to PC) - first cut built

Header, then `frame` (u32), `stage_id` (i32), then two FighterState blocks. 148 bytes, one per game frame during a match.

For now index 0 is port 1 and index 1 is port 2; bot/opponent mapping comes with SSB-6. `frame` restarts at 0 each match. Live in the first cut: fighter_kind, pos, damage, stocks (-1 if unknown), status_kind, motion_kind, anim_frame, facing, airborne flag. Sent as 0 for now: vel, shield_hp, jumps_left, stage_id, hitstun and invincible flags.

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
| jumps_left | i32 | |
| flags | u32 | bit 0 airborne, bit 1 hitstun, bit 2 invincible |

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
