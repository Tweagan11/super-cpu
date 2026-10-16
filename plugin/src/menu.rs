//! Menu presses from the PC (SSB-25). Pure logic, no skyline types.
//!
//! Menus read the raw `Controller` struct, not the `MappedInputs` the bot
//! uses in matches (docs/css-research.md section 5). A kind 8 packet asks
//! for a button mask held for `hold_frames`. The game thread (`menu_hook.rs`)
//! calls `next_pulse` once per frame for the bot's controller and ORs the
//! result into `current_buttons` and `just_down`. The press is pulsed, 1
//! frame on and 2 off, because a button that is simply held down doesn't
//! register as a fresh press in menus.
//!
//! Mask bits are the Controller button bits (the game's ButtonBitfield:
//! A = 0x20, B = 0x40, + = 0x40000), see bridge/bridge/buttons.py.

use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;

use crate::net::{HEADER_LEN, MAGIC, PROTO_VERSION};

pub const KIND_MENU: u16 = 8;
/// header + seq (u32) + mask (u32) + hold_frames (u16) + 2 reserved bytes
pub const MENU_LEN: usize = HEADER_LEN + 12;
/// Longest hold honored, in frames (a stuck press shouldn't last long).
pub const MAX_HOLD: u16 = 120;
/// Pulse period in frames: on for the first, off for the rest.
pub const PULSE: u32 = 3;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct MenuPress {
    pub seq: u32,
    pub mask: u32,
    pub hold_frames: u16,
}

pub fn parse_menu(buf: &[u8]) -> Option<MenuPress> {
    if buf.len() < MENU_LEN || buf[0..4] != MAGIC {
        return None;
    }
    if u16::from_le_bytes([buf[4], buf[5]]) != PROTO_VERSION || u16::from_le_bytes([buf[6], buf[7]]) != KIND_MENU {
        return None;
    }
    let u32_at = |i: usize| u32::from_le_bytes([buf[i], buf[i + 1], buf[i + 2], buf[i + 3]]);
    Some(MenuPress { seq: u32_at(12), mask: u32_at(16), hold_frames: u16::from_le_bytes([buf[20], buf[21]]) })
}

#[derive(Default)]
pub struct Presser {
    last_seq: Option<u32>,
    mask: u32,
    hold: u32,
    elapsed: u32,
}

impl Presser {
    pub const fn new() -> Self {
        Presser { last_seq: None, mask: 0, hold: 0, elapsed: 0 }
    }

    /// Network thread: a new press replaces whatever is running. A seq that
    /// isn't newer (a duplicate or a late packet) is ignored.
    pub fn offer(&mut self, p: MenuPress) -> bool {
        if let Some(last) = self.last_seq {
            let step = p.seq.wrapping_sub(last);
            if step == 0 || step > 0x7FFF_FFFF {
                return false;
            }
        }
        self.last_seq = Some(p.seq);
        self.mask = p.mask;
        self.hold = p.hold_frames.clamp(1, MAX_HOLD) as u32;
        self.elapsed = 0;
        true
    }

    pub fn active(&self) -> bool {
        self.mask != 0 && self.elapsed < self.hold
    }

    /// Drop the running press (we're in a match).
    pub fn cancel(&mut self) {
        self.mask = 0;
    }

    /// Game thread, once per frame for the bot's controller: the mask to OR
    /// in this frame, or None on off frames.
    pub fn tick(&mut self) -> Option<u32> {
        if !self.active() {
            return None;
        }
        let on = self.elapsed % PULSE == 0;
        self.elapsed += 1;
        on.then_some(self.mask)
    }

    pub fn last_seq(&self) -> Option<u32> {
        self.last_seq
    }
}

pub static PRESSER: Mutex<Presser> = Mutex::new(Presser::new());
/// Pulses (frames with a press written) applied since boot.
pub static APPLIED: AtomicU32 = AtomicU32::new(0);
/// Last seq that produced a pulse; u32::MAX when none yet.
pub static APPLIED_SEQ: AtomicU32 = AtomicU32::new(u32::MAX);

/// Network thread: file a kind 8 packet. False if it isn't one or was stale.
pub fn handle_packet(buf: &[u8]) -> bool {
    let Some(p) = parse_menu(buf) else { return false };
    match PRESSER.lock() {
        Ok(mut pr) => pr.offer(p),
        Err(e) => e.into_inner().offer(p),
    }
}

/// Game thread: the mask to press this frame, if any. Never waits on the lock.
pub fn next_pulse() -> Option<u32> {
    let mut p = PRESSER.try_lock().ok()?;
    let seq = p.last_seq();
    let mask = p.tick()?;
    APPLIED.fetch_add(1, Ordering::Relaxed);
    APPLIED_SEQ.store(seq.unwrap_or(u32::MAX), Ordering::Relaxed);
    Some(mask)
}

/// Game thread: forget the running press (never waits on the lock).
pub fn cancel() {
    if let Ok(mut p) = PRESSER.try_lock() {
        p.cancel();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::net::write_header;

    fn pkt(seq: u32, mask: u32, hold: u16) -> [u8; MENU_LEN] {
        let mut b = [0u8; MENU_LEN];
        write_header(&mut b, KIND_MENU, 0);
        b[12..16].copy_from_slice(&seq.to_le_bytes());
        b[16..20].copy_from_slice(&mask.to_le_bytes());
        b[20..22].copy_from_slice(&hold.to_le_bytes());
        b
    }

    #[test]
    fn parses_and_rejects() {
        assert_eq!(parse_menu(&pkt(7, 0x20, 4)), Some(MenuPress { seq: 7, mask: 0x20, hold_frames: 4 }));
        assert_eq!(parse_menu(&pkt(7, 0x20, 4)[..MENU_LEN - 1]), None);
        let mut bad = pkt(1, 1, 1);
        bad[6] = 2;
        assert_eq!(parse_menu(&bad), None);
    }

    #[test]
    fn pulses_one_on_two_off() {
        let mut p = Presser::new();
        assert!(p.offer(MenuPress { seq: 1, mask: 0x20, hold_frames: 7 }));
        let got: Vec<bool> = (0..9).map(|_| p.tick().is_some()).collect();
        assert_eq!(got, [true, false, false, true, false, false, true, false, false]);
        assert!(!p.active());
    }

    #[test]
    fn single_frame_and_clamps() {
        let mut p = Presser::new();
        p.offer(MenuPress { seq: 1, mask: 0x40000, hold_frames: 0 });
        assert_eq!(p.tick(), Some(0x40000));
        assert_eq!(p.tick(), None);
        p.offer(MenuPress { seq: 2, mask: 1, hold_frames: u16::MAX });
        assert_eq!(p.hold, MAX_HOLD as u32);
    }

    #[test]
    fn stale_seq_ignored_and_cancel() {
        let mut p = Presser::new();
        assert!(p.offer(MenuPress { seq: 5, mask: 1, hold_frames: 3 }));
        assert!(!p.offer(MenuPress { seq: 5, mask: 2, hold_frames: 3 }));
        assert!(!p.offer(MenuPress { seq: 4, mask: 2, hold_frames: 3 }));
        assert!(p.offer(MenuPress { seq: 6, mask: 2, hold_frames: 3 }));
        p.cancel();
        assert_eq!(p.tick(), None);
        let mut p = Presser::new();
        p.offer(MenuPress { seq: u32::MAX, mask: 1, hold_frames: 1 });
        assert!(p.offer(MenuPress { seq: 0, mask: 1, hold_frames: 1 }));
    }
}
