# Wire protocol

See the Phase 0 design doc for the reasoning. Short version: per-frame traffic goes over UDP because only the newest packet matters, and a small TCP channel (not built yet) handles the handshake and version check. Little endian throughout.

Plugin to PC: UDP port 4243 by default (set in `sd:/ultimate/super-cpu/config.toml`).

## Header (every UDP packet)

| Field | Type | Notes |
| --- | --- | --- |
| magic | 4 bytes | `SCPU` |
| proto_version | u16 | currently 1 |
| kind | u16 | 1 = state, 2 = input, 3 = heartbeat |
| seq | u32 | per sender, wraps at 2^32 |

12 bytes. Code: `plugin/src/net.rs`, `bridge/bridge/wire.py`.

## Heartbeat (kind 3, plugin to PC) - built

| Field | Type | Notes |
| --- | --- | --- |
| header | 12 bytes | |
| uptime_ms | u64 | since the plugin loaded; going backwards means a restart |
| game_version | 8 bytes | ASCII, NUL padded, e.g. `13.0.4` |

28 bytes, sent every 500 ms.

## State (kind 1, plugin to PC) - not built yet

Header, then `frame` (u32), `stage_id` (i32), then two FighterState blocks (index 0 = bot, 1 = opponent). 148 bytes.

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
