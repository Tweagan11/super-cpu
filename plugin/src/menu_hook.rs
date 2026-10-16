//! Game-side hooks for SSB-25: scene reporting and menu presses. See
//! docs/hooks.md for the risks. The logic lives in `scene.rs` and `menu.rs`.
//!
//! Both hooks are fixed 13.0.4 offsets from docs/css-research.md, so they are
//! only installed when the running game version matches.
//!
//! - Scene change, `0x3726120` (HDR `scene_transition`, ARCropolis
//!   `scene_queue_push_front`): read only. The target scene's name is a
//!   NUL-terminated string at `target + 8`.
//! - Controller update, `0x36650c0` (HDR `process_inputs_handheld`): after the
//!   original ran, ORs a pulsed press from the PC into the bot's `Controller`.
//!   Only with `auto_menu = true`, only outside matches.
//!
//! The `Controller` layout is the Training Modpack's (`src/common/input.rs`,
//! checked against its size below). Both hooks forward all argument registers
//! and the return value untouched, because their exact signatures are
//! unverified (the doc names the offsets, not the prototypes).

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Mutex;

use crate::{menu, scene};

const SCENE_CHANGE_OFFSET: usize = 0x3726120;
const CONTROLLER_UPDATE_OFFSET: usize = 0x36650c0;
/// The name sits right after an 8 byte header (hash and length, order unknown).
const NAME_OFFSET: usize = 8;

pub static SCENE_HOOK: AtomicBool = AtomicBool::new(false);
pub static MENU_HOOK: AtomicBool = AtomicBool::new(false);
/// Controller that gets menu presses, matched on `Controller::npad_number`.
/// AUTO: the lowest npad seen with a nonzero style (connected). The bot's port
/// is often a CPU card with no controller of its own (drive_cpu), and any
/// controller can advance results or start the match from the CSS.
const AUTO: u32 = u32::MAX;
static MENU_NPAD: AtomicU32 = AtomicU32::new(AUTO);
/// Auto mode's current pick.
static AUTO_NPAD: AtomicU32 = AtomicU32::new(AUTO);

/// Raw controller state the menus read (Training Modpack layout). Only the
/// fields we touch are named.
#[repr(C)]
pub struct Controller {
    vtable: u64,
    current_buttons: u32,
    previous_buttons: u32,
    /// sticks, triggers, gyro, autorepeat timespans (offsets 16 to 136)
    _sticks_gyro_timespans: [u8; 120],
    just_down: u32,
    just_release: u32,
    _autorepeat: [u32; 3],
    style: u32,
    controller_id: u32,
    _colors: [u32; 4],
    /// led pattern, connection flags (offsets 180 to 196)
    _flags: [u8; 16],
    npad_number: u32,
    _tail: [u8; 8],
}

/// The Modpack's struct is 0xd0 bytes; if ours isn't, the offsets are off.
const _: () = assert!(std::mem::size_of::<Controller>() == 0xd0);

unsafe fn read_name(target: *const u8) -> Vec<u8> {
    let mut out = Vec::new();
    if target.is_null() {
        return out;
    }
    for i in 0..scene::NAME_LEN {
        let b = *target.add(NAME_OFFSET + i);
        if b == 0 {
            break;
        }
        out.push(b);
    }
    out
}

#[skyline::hook(offset = SCENE_CHANGE_OFFSET)]
unsafe fn on_scene_change(queue: u64, target: *const u8, leaving: *const u8, params: u64) -> u64 {
    let new = read_name(target);
    let from = read_name(leaving);
    let before = scene::snapshot().name();
    let n = scene::set(&new);
    crate::log::log(format!(
        "scene #{n}: {:?} -> {:?} (leaving arg {:?})",
        before,
        String::from_utf8_lossy(&new),
        String::from_utf8_lossy(&from)
    ));
    original!()(queue, target, leaving, params)
}

/// Distinct (style, npad, controller_id) triples seen, logged once each so
/// experiment E7 can see which controllers pass through this function.
static SEEN: Mutex<Vec<(u32, u32, u32)>> = Mutex::new(Vec::new());

#[skyline::hook(offset = CONTROLLER_UPDATE_OFFSET)]
unsafe fn on_controller_update(ctrl: *mut Controller, a1: u64, a2: u64, a3: u64) -> u64 {
    let ret = original!()(ctrl, a1, a2, a3);
    if ctrl.is_null() {
        return ret;
    }
    let c = &mut *ctrl;
    let key = (c.style, c.npad_number, c.controller_id);
    if let Ok(mut seen) = SEEN.try_lock() {
        if seen.len() < 8 && !seen.contains(&key) {
            seen.push(key);
            crate::log::log(format!("controller update: style {} npad {} id {}", key.0, key.1, key.2));
        }
    }
    let mut target = MENU_NPAD.load(Ordering::Relaxed);
    if target == AUTO {
        if c.style != 0 && c.npad_number < AUTO_NPAD.load(Ordering::Relaxed) {
            AUTO_NPAD.store(c.npad_number, Ordering::Relaxed);
            crate::log::log(format!(
                "menu presses go to npad {} (lowest connected controller, style {}); set menu_npad to pick another",
                c.npad_number, c.style
            ));
        }
        target = AUTO_NPAD.load(Ordering::Relaxed);
    }
    if c.npad_number != target {
        return ret;
    }
    if !scene::menu_presses_allowed() || crate::state::live_frame().is_some() {
        menu::cancel();
        return ret;
    }
    if let Some(mask) = menu::next_pulse() {
        c.current_buttons |= mask;
        c.just_down |= mask;
    }
    ret
}

/// `npad` is the controller that gets menu presses (None: lowest connected),
/// `menu` turns on the press hook. The scene hook is read only and always installed.
pub fn install(npad: Option<u32>, menu: bool) {
    if !crate::version::game_version_ok() {
        crate::log::log("scene and menu hooks off: game version mismatch (fixed offsets)");
        return;
    }
    skyline::install_hook!(on_scene_change);
    SCENE_HOOK.store(true, Ordering::Relaxed);
    crate::log::log(format!("scene hook installed at text+{SCENE_CHANGE_OFFSET:#x}"));
    if menu {
        MENU_NPAD.store(npad.unwrap_or(AUTO), Ordering::Relaxed);
        skyline::install_hook!(on_controller_update);
        MENU_HOOK.store(true, Ordering::Relaxed);
        let who = npad.map_or("the lowest connected controller".to_string(), |n| format!("npad {n}"));
        crate::log::log(format!("auto_menu on: controller hook at text+{CONTROLLER_UPDATE_OFFSET:#x}, presses go to {who}"));
    } else {
        crate::log::log("auto_menu off: menu presses from the PC are ignored");
    }
}

pub fn flags() -> u32 {
    (if SCENE_HOOK.load(Ordering::Relaxed) { scene::FLAG_SCENE_HOOK } else { 0 })
        | (if MENU_HOOK.load(Ordering::Relaxed) { scene::FLAG_MENU_HOOK } else { 0 })
}
