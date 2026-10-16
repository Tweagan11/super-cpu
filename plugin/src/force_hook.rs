//! Game-side hooks for SSB-24: make the configured ports play green R.O.B.
//! without moving a cursor. Decision logic and the log throttle are in
//! `force.rs`; the risks are in docs/hooks.md section 6.
//!
//! Both hooks are fixed 13.0.4 offsets from docs/css-research.md (HDR-1304
//! `chara_select/random.rs`), so they are only installed when the running
//! game version matches.
//!
//! - "Set random fighter data", `0x1a0d540`: HDR replaces Random picks here.
//!   `base` points at the per-player decided-fighter record: `*base as u8` is
//!   the 1-based port, word 3 holds the ui_chara hash in its low 40 bits, and
//!   the byte at `+0x20` is the costume. For a forced port we write those two
//!   fields before and after the original, as HDR does.
//! - Record copy, `0x1798ac8` (inline): HDR copies words 2 to 4 from `x0` to
//!   `x1` here so the choice reaches the match. We write the forced fields
//!   into the source (`x0`) before the copy runs, so the copy carries them.
//!
//! Nothing else in the record is touched. Every pointer is null and
//! alignment checked, and the port byte must be one we were told to force.

use std::sync::atomic::{AtomicBool, Ordering};

use skyline::hooks::InlineCtx;

use crate::force::{self, COSTUME_OFFSET, DUMP_LEN, HASH_WORD, KEY_MASK};

const SET_RANDOM_OFFSET: usize = 0x1a0d540;
const COPY_RECORD_OFFSET: usize = 0x1798ac8;

pub static RANDOM_HOOK: AtomicBool = AtomicBool::new(false);
pub static COPY_HOOK: AtomicBool = AtomicBool::new(false);

const PHASE_NAMES: [&str; 3] = ["pre", "post", "copy"];

/// Reads the record, writes the forced fields when its port is forced, and
/// logs what happened. `phase`: 0 before the original, 1 after, 2 the copy.
unsafe fn process(base: *mut u64, phase: usize) {
    if base.is_null() || (base as usize) % 8 != 0 {
        return;
    }
    let w0 = base.read_volatile();
    let port = (w0 & 0xff) as u8;
    if !force::is_forced(port) {
        return;
    }
    let cost_ptr = (base as *mut u8).add(COSTUME_OFFSET);
    let word = base.add(HASH_WORD).read_volatile();
    let costume = cost_ptr.read_volatile();
    let (w2, w4) = (base.add(2).read_volatile(), base.add(4).read_volatile());
    let dump = if force::should_dump(phase) {
        let mut raw = [0u8; DUMP_LEN];
        std::ptr::copy_nonoverlapping(base as *const u8, raw.as_mut_ptr(), DUMP_LEN);
        Some(force::hex_dump(&raw))
    } else {
        None
    };

    let plan = force::plan(word, costume, force::costume());
    if plan.changes {
        base.add(HASH_WORD).write_volatile(plan.hash_word);
        cost_ptr.write_volatile(plan.costume);
    }

    let name = PHASE_NAMES[phase];
    if force::should_log(port, (word & !KEY_MASK, costume, phase as u8)) {
        crate::log::log(format!(
            "force {name}: P{port} saw hash {:#012x} c{costume:02} raw w0 {w0:#x} w2 {w2:#x} w4 {w4:#x}; {}",
            word & !KEY_MASK,
            if plan.changes {
                format!("wrote hash {:#012x} c{:02}", plan.hash_word & !KEY_MASK, plan.costume)
            } else {
                "wrote nothing (already forced)".to_string()
            }
        ));
    }
    if let Some(d) = dump {
        crate::log::log(format!("force {name} dump P{port} before: {d}"));
    }
}

#[skyline::hook(offset = SET_RANDOM_OFFSET)]
unsafe fn on_set_random(base: *mut u64, a1: u64, a2: u64, a3: u64) -> u64 {
    process(base, 0);
    let ret = original!()(base, a1, a2, a3);
    process(base, 1);
    ret
}

#[skyline::hook(offset = COPY_RECORD_OFFSET, inline)]
unsafe fn on_copy_record(ctx: &mut InlineCtx) {
    process(ctx.registers[0].x() as *mut u64, 2);
}

/// Installs nothing unless `enabled` and the game is 13.0.4.
pub fn install(enabled: bool, ports: &[u8], costume: u8, copy_hook: bool) {
    if !enabled {
        return;
    }
    if !crate::version::game_version_ok() {
        crate::log::log("force_fighter off: game version mismatch (fixed offsets)");
        return;
    }
    if ports.is_empty() {
        crate::log::log("force_fighter on but force_ports is empty, nothing to do");
        return;
    }
    force::init(ports, costume);
    skyline::install_hook!(on_set_random);
    RANDOM_HOOK.store(true, Ordering::Relaxed);
    if copy_hook {
        skyline::install_hook!(on_copy_record);
        COPY_HOOK.store(true, Ordering::Relaxed);
    }
    crate::log::log(format!(
        "force_fighter on: ports {ports:?} -> robot c{costume:02}, hook at text+{SET_RANDOM_OFFSET:#x}, copy hook {}",
        if copy_hook { format!("at text+{COPY_RECORD_OFFSET:#x}") } else { "off".to_string() }
    ));
}
