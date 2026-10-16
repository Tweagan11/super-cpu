//! Puts the PC's inputs on the bot's port (SSB-6). See docs/hooks.md for why
//! this hook and not the CPU one.
//!
//! Hooks the game's final input mapping, the function that turns one
//! controller's raw state into the `MappedInputs` the fighter reads that
//! frame. Copied from the Training Modpack, `src/training/mod.rs`
//! (`handle_final_input_mapping` at `OFFSET_FIM`), where input delay
//! (`src/training/input_delay.rs`) and input record/playback
//! (`src/training/input_record.rs`) both edit `*out` after the original runs.
//! We do the same for one player index and leave every other one alone.
//!
//! Everything that isn't game types lives in `input.rs` so it can be tested
//! on a PC.

use std::sync::atomic::{AtomicUsize, Ordering};

use crate::input::{self, Inputs};

/// The game's per-player mapped inputs. Layout from the Training Modpack,
/// `src/common/input.rs` (`MappedInputs`): u32 button bits (same bits as
/// bridge/bridge/buttons.py) then four i8 stick axes. 8 bytes.
#[derive(Clone, Copy, Debug, Default)]
#[repr(C)]
pub struct MappedInputs {
    pub buttons: u32,
    pub lstick_x: i8,
    pub lstick_y: i8,
    pub rstick_x: i8,
    pub rstick_y: i8,
}

const _: () = assert!(std::mem::size_of::<MappedInputs>() == 8);

impl From<MappedInputs> for Inputs {
    fn from(m: MappedInputs) -> Inputs {
        Inputs { stick_x: m.lstick_x, stick_y: m.lstick_y, cstick_x: m.rstick_x, cstick_y: m.rstick_y, buttons: m.buttons }
    }
}

impl From<Inputs> for MappedInputs {
    fn from(i: Inputs) -> MappedInputs {
        MappedInputs { buttons: i.buttons, lstick_x: i.stick_x, lstick_y: i.stick_y, rstick_x: i.cstick_x, rstick_y: i.cstick_y }
    }
}

/// text-relative offset of the final input mapping function, set by install()
static FIM_OFFSET: AtomicUsize = AtomicUsize::new(0);

fn fim_offset() -> usize {
    FIM_OFFSET.load(Ordering::Relaxed)
}

/// Same signature as the modpack's `handle_final_input_mapping`. The
/// controller mapping and controller struct are passed straight through, so
/// they're opaque pointers here (same ABI as the modpack's `*mut
/// ControllerMapping` and `&mut SomeControllerStruct`).
#[skyline::hook(offset = fim_offset())]
unsafe fn handle_final_input_mapping(
    mappings: *mut u8,
    player_idx: i32, // port or controller slot? Open question, see docs/hooks.md
    out: *mut MappedInputs,
    controller_struct: *mut u8,
    arg: bool,
) {
    original!()(mappings, player_idx, out, controller_struct, arg);
    if out.is_null() {
        return;
    }
    let orig = Inputs::from(*out);
    if let Some(inputs) = input::on_final_input_mapping(player_idx, orig, crate::state::live_frame()) {
        *out = MappedInputs::from(inputs);
    }
}

// ---------------------------------------------------------------------------
// CPU controls probe (read only)
//
// The modpack's `set_cpu_controls` (src/training/input_record.rs) runs after
// the CPU AI writes its controls and gets `p_data`, where `*p_data.add(1)` is
// the CPU's `ControlModuleInternal` (layout in src/common/input.rs):
//
//     vtable: *mut u8, controller_index: i32, buttons: Buttons, ...
//
// The modpack reads `controller_index` but never uses it, and it picks the
// CPU by assuming it's entry 1 in training mode. Before we can drive a CPU
// slot we need to know what controller_index is per CPU, so for now this
// hook only records which values show up (sent in the control status
// packet). It changes nothing. Turn it off with `cpu_probe = 0`.

/// Only the first two fields; we never touch the rest.
#[repr(C)]
struct ControlModuleHead {
    vtable: *mut u8,
    controller_index: i32,
}

static CPU_OFFSET: AtomicUsize = AtomicUsize::new(0);

fn cpu_offset() -> usize {
    CPU_OFFSET.load(Ordering::Relaxed)
}

/// Same signature as the modpack's `set_cpu_controls`.
#[skyline::hook(offset = cpu_offset())]
unsafe fn set_cpu_controls(p_data: *mut *mut u8) {
    call_original!(p_data);
    if p_data.is_null() {
        return;
    }
    let controller_data = *p_data.add(1) as *const ControlModuleHead;
    let idx = if controller_data.is_null() { -1 } else { (*controller_data).controller_index };
    input::on_cpu_controls(idx);
}

/// Finds the functions by byte pattern and hooks them. If a pattern isn't
/// found (game update), the plugin keeps streaming state and the bot just
/// can't move; the heartbeat's control status says so.
pub fn install(cpu_probe: bool) {
    match crate::offsets::fim() {
        Some(off) => {
            FIM_OFFSET.store(off, Ordering::Relaxed);
            skyline::install_hooks!(handle_final_input_mapping);
            input::HOOK_INSTALLED.store(true, Ordering::Relaxed);
            crate::log::log(format!("final input mapping hook installed, bot on port {}", input::bot_port()));
        }
        None => crate::log::log(format!("final input mapping not found, the PC can't drive the bot")),
    }
    if !cpu_probe {
        return;
    }
    match crate::offsets::set_cpu_controls() {
        Some(off) => {
            CPU_OFFSET.store(off, Ordering::Relaxed);
            skyline::install_hooks!(set_cpu_controls);
            input::CPU_PROBE_INSTALLED.store(true, Ordering::Relaxed);
            crate::log::log(format!("cpu controls probe installed (read only)"));
        }
        None => crate::log::log(format!("set_cpu_controls not found, cpu probe off")),
    }
}
