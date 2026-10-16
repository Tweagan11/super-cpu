//! Driving a CPU slot (drive_cpu = true). Pure logic and layouts only, no game
//! calls, so it tests on a PC. The hook that uses it is `control.rs`
//! (`set_cpu_controls`), the rationale is in docs/hooks.md section 3.
//!
//! Layout and scaling copied from the Training Modpack: `ControlModuleInternal`
//! and `Buttons` in `src/common/input.rs`, the write in `set_cpu_controls`
//! (`src/training/input_record.rs`), `STICK_NEUTRAL` and
//! `STICK_CLAMP_MULTIPLIER` next to it.

use crate::input::{stick_to_i8, Inputs};

/// The modpack's `ControlModuleInternal`: what the game reads for one fighter
/// after the CPU AI ran. Offsets: vtable 0, controller_index 8, buttons 0xc,
/// stick_x/y 0x10, unk 0x20..0x40, clamped_lstick 0x40, clamped_rstick 0x50.
#[derive(Debug, Copy, Clone)]
#[repr(C)]
pub struct ControlModuleInternal {
    pub vtable: *mut u8,
    pub controller_index: i32,
    pub buttons: u32,
    pub stick_x: f32,
    pub stick_y: f32,
    pub padding: [f32; 2],
    pub unk: [u32; 8],
    pub clamped_lstick_x: f32,
    pub clamped_lstick_y: f32,
    pub padding2: [f32; 2],
    pub clamped_rstick_x: f32,
    pub clamped_rstick_y: f32,
}

#[cfg(target_pointer_width = "64")]
const _: () = assert!(std::mem::size_of::<ControlModuleInternal>() == 0x58);

/// The modpack's picks for the clamped copy: the i8 stick over 120 (its
/// CLAMP_MAX), clamped to +-1, and zeroed under 0.2 in magnitude.
pub const STICK_CLAMP_MULTIPLIER: f32 = 1.0 / 120.0;
pub const STICK_NEUTRAL: f32 = 0.2;

/// Raw stick: i8 / 127, as the modpack's playback does.
pub fn stick_raw(v: i8) -> f32 {
    v as f32 / i8::MAX as f32
}

/// Clamped stick: i8 / 120 clamped to +-1, zero inside the deadzone.
pub fn stick_clamped(v: i8) -> f32 {
    let c = (v as f32 * STICK_CLAMP_MULTIPLIER).clamp(-1.0, 1.0);
    if c.abs() >= STICK_NEUTRAL {
        c
    } else {
        0.0
    }
}

/// What `stick_to_i8(get_stick_x(..))` should read back after we wrote `v`,
/// assuming the game reports the clamped copy. For the agreement check only.
pub fn expected_cm_stick(v: i8) -> i8 {
    stick_to_i8(stick_clamped(v))
}

/// The main button bits that the agreement check compares (attack, special,
/// jump, guard). The rest are derived flags whose read-back isn't verified.
pub const AGREE_BUTTONS: u32 = 0xf;

/// Do the inputs we wrote and the inputs the fighter's ControlModule reported
/// agree? Sticks within 2 of the converted value, the four main buttons equal.
pub fn cm_agrees(applied: Inputs, cm: Inputs) -> bool {
    let near = |a: i8, b: i8| (expected_cm_stick(a) as i32 - b as i32).abs() <= 2;
    near(applied.stick_x, cm.stick_x)
        && near(applied.stick_y, cm.stick_y)
        && near(applied.cstick_x, cm.cstick_x)
        && near(applied.cstick_y, cm.cstick_y)
        && applied.buttons & AGREE_BUTTONS == cm.buttons & AGREE_BUTTONS
}

impl ControlModuleInternal {
    /// What the CPU AI decided, in our i8 / MappedInputs-bit terms.
    pub fn read(&self) -> Inputs {
        Inputs {
            stick_x: stick_to_i8(self.stick_x),
            stick_y: stick_to_i8(self.stick_y),
            cstick_x: stick_to_i8(self.clamped_rstick_x),
            cstick_y: stick_to_i8(self.clamped_rstick_y),
            buttons: self.buttons,
        }
    }

    /// Replace the AI's decision. Buttons are written as-is: the modpack's
    /// `Buttons` bitflags and our `MappedInputs.buttons` are the same bits
    /// (checked in the tests below against buttons.py's values).
    pub fn write(&mut self, i: Inputs) {
        self.buttons = i.buttons;
        self.stick_x = stick_raw(i.stick_x);
        self.stick_y = stick_raw(i.stick_y);
        self.clamped_lstick_x = stick_clamped(i.stick_x);
        self.clamped_lstick_y = stick_clamped(i.stick_y);
        // The modpack never writes these. They sit after padding2 in the same
        // block and it reads them back (`is_input_neutral`) as the c-stick, so
        // this is a best guess; docs/testing-phase0.md step 14 checks it.
        self.clamped_rstick_x = stick_clamped(i.cstick_x);
        self.clamped_rstick_y = stick_clamped(i.cstick_y);
    }
}

/// Which `controller_index` is the bot's CPU, from the indices the hook was
/// called with.
///
/// - `override_idx` (config `cpu_controller_index`) wins.
/// - One CPU in the match (one distinct index): that one is the bot.
/// - Several (CPU vs CPU): sort ascending and take rank `bot_port - 1`,
///   assuming the hook's indices rise with the port. Wrong if the other CPU
///   sits on a higher port than the bot's rank implies; use the override.
/// - Rank past the end, or nothing seen yet: None (don't drive).
pub fn pick_cpu_index(seen: u64, bot_port: u8, override_idx: Option<i32>) -> Option<i32> {
    if override_idx.is_some() {
        return override_idx;
    }
    match seen.count_ones() {
        0 => None,
        1 => Some(seen.trailing_zeros() as i32),
        n => {
            let rank = (bot_port as u32).saturating_sub(1);
            if rank >= n {
                return None;
            }
            let mut s = seen;
            for _ in 0..rank {
                s &= s - 1; // drop the lowest set bit
            }
            Some(s.trailing_zeros() as i32)
        }
    }
}

/// Learns the bot's controller_index from the calls of the previous game
/// frame. Using last frame's complete set (not the calls so far this frame)
/// matters in CPU vs CPU: the first call of a frame alone looks like "one
/// CPU". Costs one frame at match start, where the AI keeps control.
pub struct CpuTracker {
    frame: Option<u32>,
    current: u64,
    settled: u64,
    announced: Option<i32>,
}

pub struct Decision {
    /// Write the PC's inputs into this call's controls.
    pub drive: bool,
    /// Set the first time the bot's index is known or when it changes, for the log.
    pub announce: Option<i32>,
    /// Distinct indices seen last frame, for the log.
    pub seen: u64,
}

fn bit(idx: i32) -> u64 {
    if (0..64).contains(&idx) {
        1 << idx
    } else {
        0
    }
}

impl CpuTracker {
    pub const fn new() -> Self {
        CpuTracker { frame: None, current: 0, settled: 0, announced: None }
    }

    /// `target` is the frame the written controls are for (live_frame + 1).
    pub fn observe(&mut self, target: u32, idx: i32, bot_port: u8, override_idx: Option<i32>) -> Decision {
        if let Some(f) = self.frame {
            if target < f {
                *self = CpuTracker::new(); // new match, frame counter restarted
            }
        }
        if self.frame != Some(target) {
            if self.current != 0 {
                self.settled = self.current;
            }
            self.current = 0;
            self.frame = Some(target);
        }
        self.current |= bit(idx);
        let chosen = pick_cpu_index(self.settled, bot_port, override_idx);
        let announce = if chosen.is_some() && chosen != self.announced {
            self.announced = chosen;
            chosen
        } else {
            None
        };
        Decision { drive: chosen == Some(idx), announce, seen: self.settled }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn layout() {
        assert_eq!(std::mem::size_of::<ControlModuleInternal>(), 0x58);
        let m: ControlModuleInternal = unsafe { std::mem::zeroed() };
        let base = &m as *const _ as usize;
        let off = |p: *const f32| p as usize - base;
        assert_eq!(off(&m.stick_x), 0x10);
        assert_eq!(off(&m.clamped_lstick_x), 0x40);
        assert_eq!(off(&m.clamped_rstick_x), 0x50);
    }

    #[test]
    fn button_bits_are_the_modpack_ones() {
        // Buttons bitflags from the modpack's src/common/input.rs, written out.
        let modpack = [0x1, 0x2, 0x4, 0x8, 0x10, 0x20, 0x40, 0x80, 0x100, 0x200, 0x400, 0x800, 0x1000, 0x2000, 0x4000,
            0x8000, 0x10000, 0x20000u32];
        let mut ours: Vec<u32> = crate::input::PAD_BUTTONS.iter().map(|b| b.1).collect();
        ours.sort_unstable();
        assert_eq!(ours, modpack.to_vec());
        let mut m: ControlModuleInternal = unsafe { std::mem::zeroed() };
        m.write(Inputs { buttons: 0x20005, ..Inputs::NEUTRAL });
        assert_eq!(m.buttons, 0x20005, "written untouched");
    }

    #[test]
    fn stick_conversion() {
        assert_eq!(stick_raw(127), 1.0);
        assert_eq!(stick_raw(-127), -1.0);
        assert_eq!(stick_raw(0), 0.0);
        assert_eq!(stick_clamped(120), 1.0);
        assert_eq!(stick_clamped(127), 1.0, "clamped to 1");
        assert_eq!(stick_clamped(-128), -1.0);
        assert_eq!(stick_clamped(23), 0.0, "inside the 0.2 deadzone");
        assert!((stick_clamped(24) - 0.2).abs() < 1e-6);
        assert!((stick_clamped(60) - 0.5).abs() < 1e-6);
    }

    #[test]
    fn write_and_read_back() {
        let mut m: ControlModuleInternal = unsafe { std::mem::zeroed() };
        m.controller_index = 3;
        let i = Inputs { stick_x: 127, stick_y: -64, cstick_x: 0, cstick_y: 120, buttons: 0x1 };
        m.write(i);
        assert_eq!(m.controller_index, 3, "index untouched");
        assert_eq!((m.stick_x, m.clamped_lstick_x, m.clamped_rstick_y), (1.0, 1.0, 1.0));
        let r = m.read();
        assert_eq!((r.stick_x, r.cstick_y, r.buttons), (127, 127, 1));
        assert!(cm_agrees(i, Inputs { stick_x: 127, stick_y: -68, cstick_x: 0, cstick_y: 127, buttons: 0x1 }));
        assert!(!cm_agrees(i, Inputs { stick_x: 0, ..Inputs::NEUTRAL }));
        assert!(!cm_agrees(Inputs::NEUTRAL, Inputs { buttons: 1, ..Inputs::NEUTRAL }));
        assert!(cm_agrees(Inputs { stick_x: 10, ..Inputs::NEUTRAL }, Inputs::NEUTRAL), "deadzone reads as 0");
    }

    #[test]
    fn picks() {
        assert_eq!(pick_cpu_index(0, 2, None), None);
        assert_eq!(pick_cpu_index(0b100, 2, None), Some(2), "one CPU is the bot");
        assert_eq!(pick_cpu_index(0b100, 1, None), Some(2));
        assert_eq!(pick_cpu_index(0b0110, 1, None), Some(1), "CPU vs CPU, port 1 is the lowest");
        assert_eq!(pick_cpu_index(0b0110, 2, None), Some(2));
        assert_eq!(pick_cpu_index(0b0110, 3, None), None);
        assert_eq!(pick_cpu_index(0b0110, 2, Some(5)), Some(5), "override wins");
        assert_eq!(pick_cpu_index(0, 2, Some(0)), Some(0));
        assert_eq!(pick_cpu_index(1 << 63, 2, None), Some(63));
    }

    #[test]
    fn tracker_one_cpu() {
        let mut t = CpuTracker::new();
        let d = t.observe(10, 1, 2, None);
        assert!(!d.drive && d.announce.is_none(), "first frame: not known yet");
        let d = t.observe(11, 1, 2, None);
        assert!(d.drive);
        assert_eq!(d.announce, Some(1));
        assert!(t.observe(11, 1, 2, None).announce.is_none(), "announced once");
        assert!(t.observe(12, 1, 2, None).drive);
    }

    #[test]
    fn tracker_two_cpus_waits_for_full_frame() {
        let mut t = CpuTracker::new();
        t.observe(10, 0, 2, None);
        t.observe(10, 1, 2, None);
        // frame 11: the first call (idx 0) must not be mistaken for "the only CPU"
        let a = t.observe(11, 0, 2, None);
        let b = t.observe(11, 1, 2, None);
        assert!(!a.drive && b.drive);
        assert_eq!(a.announce, Some(1), "known from the first call of the frame");
        assert!(b.announce.is_none());
        assert!(!t.observe(12, 0, 2, None).drive);
        assert!(t.observe(12, 1, 2, None).drive);
    }

    #[test]
    fn tracker_resets_on_new_match_and_override() {
        let mut t = CpuTracker::new();
        t.observe(500, 2, 2, None);
        assert!(t.observe(501, 2, 2, None).drive);
        // new match: frame counter restarts, now the CPU has another index
        t.observe(1, 3, 2, None);
        let d = t.observe(2, 3, 2, None);
        assert!(d.drive);
        assert_eq!(d.announce, Some(3));
        let mut o = CpuTracker::new();
        assert!(o.observe(1, 4, 2, Some(4)).drive, "override drives at once");
        assert!(!o.observe(1, 5, 2, Some(4)).drive);
    }
}
