//! Once-per-fighter-per-frame hook (SSB-2/SSB-3).
//!
//! Same technique as the Training Modpack (`src/training/mod.rs`,
//! `fighter_vtable_on_frame`): overwrite the fighter vtable's on-frame entry
//! with our function. Unlike the modpack, we save whatever pointer was in the
//! slot and call that first, so if another plugin patched it before us, its
//! code still runs.

use std::sync::atomic::{AtomicUsize, Ordering};

use skyline::hooks::{getRegionAddress, Region};

/// Offset (from .text) of the fighter vtable's on-frame slot.
/// From Training Modpack `src/common/offsets.rs`. Verify on 13.0.4 (SSB-2).
const FIGHTER_VTABLE_ON_FRAME_SLOT: usize = 0x4f80568;

static PREVIOUS: AtomicUsize = AtomicUsize::new(0);

type OnFrame = unsafe extern "C" fn(&mut smash::app::Fighter);

unsafe extern "C" fn on_frame(fighter: &mut smash::app::Fighter) {
    let prev = PREVIOUS.load(Ordering::Relaxed);
    if prev != 0 {
        let prev: OnFrame = std::mem::transmute(prev);
        prev(fighter);
    }
    let acc = fighter.battle_object.module_accessor;
    if !acc.is_null() {
        crate::state::on_fighter_frame(&mut *acc);
    }
}

pub fn install() {
    unsafe {
        let text = getRegionAddress(Region::Text) as usize;
        let slot = (text + FIGHTER_VTABLE_ON_FRAME_SLOT) as *const usize;
        let prev = *slot;
        PREVIOUS.store(prev, Ordering::Relaxed);
        println!(
            "[super-cpu] on-frame slot held text+{:#x}, installing hook",
            prev.wrapping_sub(text)
        );

        if skyline::patching::Patch::in_text(FIGHTER_VTABLE_ON_FRAME_SLOT)
            .data(on_frame as *const () as u64)
            .is_err()
        {
            println!("[super-cpu] failed to patch the on-frame slot, no state will be sent");
        }
    }
}
