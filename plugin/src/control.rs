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

use std::sync::atomic::{AtomicI32, AtomicUsize, Ordering};
use std::sync::Mutex;

use crate::cpu_drive::{ControlModuleInternal, CpuTracker};
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
// CPU controls: probe, and with drive_cpu the bot's controls
//
// The modpack's `set_cpu_controls` (src/training/input_record.rs) runs after
// the CPU AI writes its controls and gets `p_data`, where `*p_data.add(1)` is
// that CPU's `ControlModuleInternal` (layout in cpu_drive.rs). It's called
// once per CPU per frame. The modpack picks "the CPU" by assuming it's entry 1
// in training mode; we use `controller_index` instead (cpu_drive.rs, docs/
// hooks.md section 3).
//
// Always: record which controller_index values show up (the control status
// packet). With drive_cpu = true: for the bot's CPU, replace the AI's
// decision with the PC's inputs. Turn the probe off with `cpu_probe = 0`
// (drive_cpu installs the hook regardless).

static CPU_OFFSET: AtomicUsize = AtomicUsize::new(0);
/// Config `cpu_controller_index`, i32::MIN when unset.
static CPU_OVERRIDE: AtomicI32 = AtomicI32::new(i32::MIN);
static TRACKER: Mutex<CpuTracker> = Mutex::new(CpuTracker::new());

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
    let module = *p_data.add(1) as *mut ControlModuleInternal;
    if module.is_null() {
        input::on_cpu_controls(-1);
        return;
    }
    let idx = (*module).controller_index;
    input::on_cpu_controls(idx);
    if input::drive_cpu() {
        drive(&mut *module, idx);
    }
}

/// drive_cpu, game thread. Is this call the bot's CPU, and if so write the
/// PC's inputs for the frame the next fighter update reads.
fn drive(module: &mut ControlModuleInternal, idx: i32) {
    // None outside a match. (The written inputs are filed under `live`
    // itself, see input::on_cpu_drive; `target` here only drives the tracker.)
    let Some(live) = crate::state::live_frame() else { return };
    let target = live.wrapping_add(1);
    let ovr = CPU_OVERRIDE.load(Ordering::Relaxed);
    let ovr = (ovr != i32::MIN).then_some(ovr);
    let d = {
        let mut t = match TRACKER.lock() {
            Ok(t) => t,
            Err(p) => p.into_inner(),
        };
        t.observe(target, idx, input::bot_port(), ovr)
    };
    if let Some(chosen) = d.announce {
        crate::log::log(format!(
            "drive_cpu: P{} is controller_index {} (indices seen last frame {:#x}{})",
            input::bot_port(),
            chosen,
            d.seen,
            if ovr.is_some() { ", from cpu_controller_index" } else { "" }
        ));
    }
    if !d.drive {
        return;
    }
    if let Some(i) = input::on_cpu_drive(module.read(), Some(live)) {
        module.write(i);
    }
}

/// Finds the functions by byte pattern and hooks them. If a pattern isn't
/// found (game update), the plugin keeps streaming state and the bot just
/// can't move; the heartbeat's control status says so.
pub fn install(cpu_probe: bool, drive_cpu: bool, cpu_controller_index: Option<i32>) {
    input::set_drive_cpu(drive_cpu);
    CPU_OVERRIDE.store(cpu_controller_index.unwrap_or(i32::MIN), Ordering::Relaxed);
    // drive_cpu replaces the FIM hook for the bot's port, but the FIM hook
    // still captures the other ports, so it's installed either way.
    match crate::offsets::fim() {
        Some(off) => {
            FIM_OFFSET.store(off, Ordering::Relaxed);
            skyline::install_hooks!(handle_final_input_mapping);
            input::HOOK_INSTALLED.store(true, Ordering::Relaxed);
            crate::log::log(format!("final input mapping hook installed, bot on port {}", input::bot_port()));
        }
        None => crate::log::log(format!("final input mapping not found, the PC can't drive the bot")),
    }
    if !cpu_probe && !drive_cpu {
        return;
    }
    match crate::offsets::set_cpu_controls() {
        Some(off) => {
            CPU_OFFSET.store(off, Ordering::Relaxed);
            skyline::install_hooks!(set_cpu_controls);
            input::CPU_PROBE_INSTALLED.store(true, Ordering::Relaxed);
            if drive_cpu {
                crate::log::log(format!(
                    "drive_cpu on: the PC drives the CPU on port {} through set_cpu_controls{}",
                    input::bot_port(),
                    cpu_controller_index.map_or(String::new(), |i| format!(", controller_index forced to {i}"))
                ));
            } else {
                crate::log::log(format!("cpu controls probe installed (read only)"));
            }
        }
        None => crate::log::log(format!(
            "set_cpu_controls not found, {}",
            if drive_cpu { "drive_cpu can't work" } else { "cpu probe off" }
        )),
    }
}
