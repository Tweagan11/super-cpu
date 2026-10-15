//! Once-per-fighter-per-frame hook (SSB-2/SSB-3).
//!
//! Hooks `L2CFighterCommon::sys_line_system_control_fighter` by symbol name,
//! which every fighter runs once per frame. It lives in the game's "common"
//! module, so the hook is installed when that module loads (needs the
//! nro_hook plugin, which ARCropolis setups already have). This is the same
//! pattern the Training Modpack uses for its common-module hooks, and unlike
//! patching a vtable address it doesn't break across game updates or fight
//! with other plugins hooking the same function.

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};

use skyline::nro::{self, NroInfo};
use smash::lib::L2CValue;
use smash::lua2cpp::L2CFighterCommon;

/// Reported in the heartbeat so the PC can see if the hook is alive.
pub static HOOK_INSTALLED: AtomicBool = AtomicBool::new(false);
pub static FIGHTER_CALLS: AtomicU32 = AtomicU32::new(0);

#[skyline::hook(replace = smash::lua2cpp::L2CFighterCommon_sys_line_system_control_fighter)]
unsafe fn on_fighter_frame(fighter: &mut L2CFighterCommon) -> L2CValue {
    let ret = original!()(fighter);
    FIGHTER_CALLS.fetch_add(1, Ordering::Relaxed);
    let acc = fighter.fighter_base.agent_base.agent.module_accessor;
    if !acc.is_null() {
        crate::state::on_fighter_frame(&mut *acc);
    }
    ret
}

fn nro_main(nro: &NroInfo<'_>) {
    if nro.module.isLoaded {
        return;
    }
    if nro.name == "common" {
        skyline::install_hooks!(on_fighter_frame);
        HOOK_INSTALLED.store(true, Ordering::Relaxed);
        println!("[super-cpu] common module loaded, fighter frame hook installed");
    }
}

pub fn install() {
    if nro::add_hook(nro_main).is_err() {
        println!("[super-cpu] nro_hook plugin missing, no state will be sent");
    }
}
