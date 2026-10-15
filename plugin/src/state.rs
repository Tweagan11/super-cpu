//! Per-frame state sent to the bridge. Field order and types must match
//! `bridge/bridge/protocol.py` and `docs/protocol.md`.

#[repr(C)]
#[derive(Clone, Copy, Default, Debug)]
pub struct FighterState {
    pub fighter_kind: i32,
    pub pos_x: f32,
    pub pos_y: f32,
    pub vel_x: f32,
    pub vel_y: f32,
    pub damage: f32,
    pub shield_hp: f32,
    pub stocks: i32,
    pub status_kind: i32,
    pub motion_kind: u64,
    pub anim_frame: f32,
    pub facing: i32,
    pub jumps_left: i32,
    /// bit 0 airborne, bit 1 in hitstun, bit 2 invincible
    pub flags: u32,
}

#[repr(C)]
#[derive(Clone, Copy, Default, Debug)]
pub struct FrameState {
    pub frame: u32,
    pub stage_id: i32,
    /// index 0 = bot port, index 1 = opponent
    pub fighters: [FighterState; 2],
}

// TODO(SSB-3): fill these from the game each frame.
