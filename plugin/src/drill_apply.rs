//! Game side of drills (SSB-32): place a fighter as a reset asks. Called from
//! `state::on_fighter_frame` for each fighter, on the game thread.
//!
//! Calls follow the Training Modpack's save state load
//! (`src/training/save_states.rs`, PosMove and set_damage): clear speed, set
//! position and facing, change status to WAIT (ground) or FALL (air), then
//! set the percent with `DamageModule::heal(-current)` + `add_damage`. The
//! modpack uses `change_status_request` and checks again next frame; we use
//! `change_status_force` so a fighter in hitstun or mid attack is pulled out
//! at once. Jumps are the JUMP_COUNT instance work int the state reader uses.
//!
//! Gate: only with `drills = true` in config.toml, and only in training mode
//! unless `drills_in_versus = true`.

use std::sync::atomic::{AtomicBool, Ordering};

use smash::app::{lua_bind::*, BattleObjectModuleAccessor};
use smash::lib::lua_const::*;
use smash::phx::Vector3f;

use crate::drill::{self, PENDING};

pub static ENABLED: AtomicBool = AtomicBool::new(false);
pub static IN_VERSUS: AtomicBool = AtomicBool::new(false);

extern "C" {
    /// Same symbol the Training Modpack declares (common/mod.rs).
    #[link_name = "\u{1}_ZN3app9smashball16is_training_modeEv"]
    fn is_training_mode() -> bool;
}

pub fn install(enabled: bool, in_versus: bool) {
    ENABLED.store(enabled, Ordering::Relaxed);
    IN_VERSUS.store(in_versus, Ordering::Relaxed);
    if enabled {
        crate::log::log(format!(
            "drills on: resets from the PC place fighters ({})",
            if in_versus { "training mode and versus" } else { "training mode only" }
        ));
    }
}

pub fn training_mode() -> bool {
    unsafe { is_training_mode() }
}

unsafe fn respawning(acc: &mut BattleObjectModuleAccessor) -> bool {
    let s = StatusModule::status_kind(acc);
    s == *FIGHTER_STATUS_KIND_DEAD || s == *FIGHTER_STATUS_KIND_REBIRTH || s == *FIGHTER_STATUS_KIND_STANDBY
        || s == *FIGHTER_STATUS_KIND_ENTRY
}

/// Place this fighter if a reset is waiting for its port (`entry`, 0 or 1).
pub unsafe fn on_fighter(acc: &mut BattleObjectModuleAccessor, entry: i32) {
    let port = entry as usize;
    let mut p = match PENDING.try_lock() {
        Ok(p) => p,
        Err(_) => return, // the network thread is filing a new one: next frame
    };
    if !p.active() {
        return;
    }
    let training = training_mode();
    let flags = if training { drill::DONE_FLAG_TRAINING } else { 0 };
    if !ENABLED.load(Ordering::Relaxed) || !(training || IN_VERSUS.load(Ordering::Relaxed)) {
        if let Some(seq) = p.refuse() {
            drill::finish(seq, 0, flags | drill::DONE_FLAG_REFUSED);
            crate::log::log(if ENABLED.load(Ordering::Relaxed) {
                "drill reset refused: not in training mode (set drills_in_versus = true to allow it)"
            } else {
                "drill reset refused: drills are off (set drills = true in config.toml)"
            });
        }
        return;
    }
    let Some((seq, r)) = p.take(port) else { return };
    if respawning(acc) {
        p.put_back(port, seq, r);
        return;
    }
    place(acc, &r);
    if let Some((seq, mask)) = p.placed(port, seq) {
        drill::finish(seq, mask, flags);
    }
}

unsafe fn place(acc: &mut BattleObjectModuleAccessor, r: &drill::PortReset) {
    KineticModule::clear_speed_all(acc);
    PostureModule::set_pos(acc, &Vector3f { x: r.x, y: r.y, z: 0.0 });
    if r.facing != 0 {
        PostureModule::set_lr(acc, if r.facing < 0 { -1.0 } else { 1.0 });
    }
    let status = if r.start == drill::START_GROUND { *FIGHTER_STATUS_KIND_WAIT } else { *FIGHTER_STATUS_KIND_FALL };
    StatusModule::change_status_force(acc, status, false);
    if r.damage >= 0.0 {
        DamageModule::heal(acc, -DamageModule::damage(acc, 0), 0);
        DamageModule::add_damage(acc, r.damage, 0);
    }
    if r.jumps_left >= 0 {
        let max = WorkModule::get_int(acc, *FIGHTER_INSTANCE_WORK_ID_INT_JUMP_COUNT_MAX);
        WorkModule::set_int(acc, drill::jump_count_for(max, r.jumps_left as i32), *FIGHTER_INSTANCE_WORK_ID_INT_JUMP_COUNT);
    }
}
