# Wire protocol

TCP, plugin connects to the bridge. Little endian. One state message per frame from the plugin, one input message per frame back.

## State message (plugin to bridge)

| Field | Type |
| --- | --- |
| frame | u32 |
| stage_id | i32 |
| fighters[2] | FighterState (index 0 = bot, 1 = opponent) |

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

## Input message (bridge to plugin)

| Field | Type |
| --- | --- |
| stick_x, stick_y | i8 |
| cstick_x, cstick_y | i8 |
| buttons | u32 bitmask |
| frame | u32 (the state frame this answers) |

Any change here means changing `plugin/src/state.rs` and `bridge/bridge/protocol.py` in the same commit.
