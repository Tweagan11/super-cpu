//! Drills (SSB-32): put fighters in a chosen spot on command. Pure logic, no
//! skyline types; the game-side part is `drill_apply.rs`.
//!
//! The PC sends a reset (kind 9) saying, per port, where to put the fighter,
//! facing, percent, jumps left and whether it starts on the ground or in the
//! air. The receive thread files it here; the game thread takes each port's
//! part in that fighter's next update and applies it; once every requested
//! port is placed, the send loop reports it with a reset-done packet
//! (kind 10) carrying the same reset seq. Layout: docs/protocol.md.

use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;

use crate::net::{write_header, HEADER_LEN, MAGIC, PROTO_VERSION};

pub const KIND_RESET: u16 = 9;
pub const KIND_RESET_DONE: u16 = 10;
/// One port's block in a reset packet.
pub const PORT_LEN: usize = 24;
/// header + reset seq (u32) + 2 port blocks
pub const RESET_LEN: usize = HEADER_LEN + 4 + 2 * PORT_LEN;
/// header + reset seq (u32) + placed ports mask (u32) + flags (u32)
pub const DONE_LEN: usize = HEADER_LEN + 12;

/// Start on the ground (status WAIT) or in the air (status FALL).
pub const START_GROUND: u8 = 0;
pub const START_AIR: u8 = 1;

/// reset-done flags bit 0: the game is in training mode
pub const DONE_FLAG_TRAINING: u32 = 1;
/// reset-done flags bit 1: the reset was refused (drills off, or not in
/// training mode without drills_in_versus); nothing was placed
pub const DONE_FLAG_REFUSED: u32 = 2;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct PortReset {
    pub start: u8,
    /// -1 face left, 1 face right, 0 keep
    pub facing: i8,
    /// jumps left after the reset; negative keeps the current count
    pub jumps_left: i8,
    pub x: f32,
    pub y: f32,
    /// percent after the reset; negative keeps the current percent
    pub damage: f32,
    /// launch speed (units/frame). Not applied yet: carried so the packet
    /// doesn't change when it is.
    pub vx: f32,
    pub vy: f32,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Reset {
    pub seq: u32,
    pub ports: [Option<PortReset>; 2],
}

fn f32_at(b: &[u8], i: usize) -> f32 {
    f32::from_le_bytes([b[i], b[i + 1], b[i + 2], b[i + 3]])
}

pub fn parse_reset(buf: &[u8]) -> Option<Reset> {
    if buf.len() < RESET_LEN || buf[0..4] != MAGIC {
        return None;
    }
    if u16::from_le_bytes([buf[4], buf[5]]) != PROTO_VERSION || u16::from_le_bytes([buf[6], buf[7]]) != KIND_RESET {
        return None;
    }
    let seq = u32::from_le_bytes([buf[12], buf[13], buf[14], buf[15]]);
    let mut ports = [None, None];
    for (p, slot) in ports.iter_mut().enumerate() {
        let b = &buf[16 + p * PORT_LEN..16 + (p + 1) * PORT_LEN];
        if b[0] & 1 == 0 {
            continue;
        }
        let r = PortReset {
            start: b[1],
            facing: b[2] as i8,
            jumps_left: b[3] as i8,
            x: f32_at(b, 4),
            y: f32_at(b, 8),
            damage: f32_at(b, 12),
            vx: f32_at(b, 16),
            vy: f32_at(b, 20),
        };
        if !(r.x.is_finite() && r.y.is_finite() && r.damage.is_finite()) {
            return None;
        }
        *slot = Some(r);
    }
    Some(Reset { seq, ports })
}

/// The reset being placed: what's still waiting per port and what's done.
#[derive(Default)]
pub struct Pending {
    seq: u32,
    waiting: [Option<PortReset>; 2],
    requested: u32,
    placed: u32,
    active: bool,
}

impl Pending {
    pub const fn new() -> Self {
        Pending { seq: 0, waiting: [None, None], requested: 0, placed: 0, active: false }
    }

    /// Network thread: a new reset replaces one still in progress.
    pub fn offer(&mut self, r: Reset) {
        self.seq = r.seq;
        self.waiting = r.ports;
        self.requested = r.ports.iter().enumerate().filter(|(_, p)| p.is_some()).map(|(i, _)| 1u32 << i).sum();
        self.placed = 0;
        self.active = true;
    }

    /// Game thread: this port's part, if it's waiting.
    pub fn take(&mut self, port: usize) -> Option<(u32, PortReset)> {
        let r = self.waiting.get_mut(port)?.take()?;
        Some((self.seq, r))
    }

    /// Game thread: the part taken for `port` is in place. Returns the
    /// finished reset (seq, placed mask) once every requested port is done.
    pub fn placed(&mut self, port: usize, seq: u32) -> Option<(u32, u32)> {
        if !self.active || seq != self.seq {
            return None; // a newer reset came in meanwhile
        }
        self.placed |= 1 << port;
        if self.placed & self.requested == self.requested {
            self.active = false;
            return Some((self.seq, self.placed));
        }
        None
    }

    /// Game thread: couldn't place it now (dead, respawning); try next frame.
    pub fn put_back(&mut self, port: usize, seq: u32, r: PortReset) {
        if self.active && seq == self.seq && self.waiting[port].is_none() {
            self.waiting[port] = Some(r);
        }
    }

    /// Drop it (refused): the reset is reported done with nothing placed.
    pub fn refuse(&mut self) -> Option<u32> {
        if !self.active {
            return None;
        }
        self.active = false;
        self.waiting = [None, None];
        Some(self.seq)
    }

    pub fn active(&self) -> bool {
        self.active
    }
}

pub static PENDING: Mutex<Pending> = Mutex::new(Pending::new());

/// Last finished reset, for the send loop: seq, placed mask, flags. A
/// counter makes each completion distinct even if the seq repeats.
pub static DONE_COUNT: AtomicU32 = AtomicU32::new(0);
pub static DONE_SEQ: AtomicU32 = AtomicU32::new(0);
pub static DONE_MASK: AtomicU32 = AtomicU32::new(0);
pub static DONE_FLAGS: AtomicU32 = AtomicU32::new(0);

pub fn finish(seq: u32, mask: u32, flags: u32) {
    DONE_SEQ.store(seq, Ordering::Relaxed);
    DONE_MASK.store(mask, Ordering::Relaxed);
    DONE_FLAGS.store(flags, Ordering::Relaxed);
    DONE_COUNT.fetch_add(1, Ordering::Release);
}

/// Network thread: file a kind 9 packet. False if it isn't one.
pub fn handle_packet(buf: &[u8]) -> bool {
    let Some(r) = parse_reset(buf) else { return false };
    match PENDING.lock() {
        Ok(mut p) => p.offer(r),
        Err(e) => e.into_inner().offer(r),
    }
    true
}

pub fn done_packet(seq: u32, reset_seq: u32, mask: u32, flags: u32) -> [u8; DONE_LEN] {
    let mut b = [0u8; DONE_LEN];
    write_header(&mut b, KIND_RESET_DONE, seq);
    b[12..16].copy_from_slice(&reset_seq.to_le_bytes());
    b[16..20].copy_from_slice(&mask.to_le_bytes());
    b[20..24].copy_from_slice(&flags.to_le_bytes());
    b
}

/// Jump count work value that leaves `jumps_left` jumps (clamped to 0..=max).
pub fn jump_count_for(max: i32, jumps_left: i32) -> i32 {
    (max - jumps_left.clamp(0, max.max(0))).max(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    pub fn reset_packet(seq: u32, ports: [Option<PortReset>; 2]) -> [u8; RESET_LEN] {
        let mut b = [0u8; RESET_LEN];
        write_header(&mut b, KIND_RESET, 0);
        b[12..16].copy_from_slice(&seq.to_le_bytes());
        for (p, r) in ports.iter().enumerate() {
            let Some(r) = r else { continue };
            let o = 16 + p * PORT_LEN;
            b[o] = 1;
            b[o + 1] = r.start;
            b[o + 2] = r.facing as u8;
            b[o + 3] = r.jumps_left as u8;
            for (i, v) in [r.x, r.y, r.damage, r.vx, r.vy].iter().enumerate() {
                b[o + 4 + 4 * i..o + 8 + 4 * i].copy_from_slice(&v.to_le_bytes());
            }
        }
        b
    }

    fn pr(x: f32) -> PortReset {
        PortReset { start: START_AIR, facing: -1, jumps_left: 1, x, y: -30.0, damage: 60.0, vx: 0.0, vy: 0.0 }
    }

    #[test]
    fn layout_fits_the_receive_buffer() {
        assert_eq!(RESET_LEN, 64);
        assert_eq!(DONE_LEN, 24);
    }

    #[test]
    fn parse_round_trip_and_rejects() {
        let pkt = reset_packet(7, [None, Some(pr(120.0))]);
        assert_eq!(parse_reset(&pkt), Some(Reset { seq: 7, ports: [None, Some(pr(120.0))] }));
        assert_eq!(parse_reset(&pkt[..RESET_LEN - 1]), None);
        let mut bad = pkt;
        bad[6] = 8; // menu kind
        assert_eq!(parse_reset(&bad), None);
        let mut nan = reset_packet(1, [Some(pr(f32::NAN)), None]);
        assert_eq!(parse_reset(&nan), None);
        nan[16] = 0; // port not applied: its garbage is ignored
        assert_eq!(parse_reset(&nan).unwrap().ports, [None, None]);
    }

    #[test]
    fn pending_finishes_when_every_port_is_placed() {
        let mut p = Pending::new();
        p.offer(Reset { seq: 3, ports: [Some(pr(0.0)), Some(pr(100.0))] });
        let (s, r) = p.take(1).unwrap();
        assert_eq!((s, r.x), (3, 100.0));
        assert!(p.take(1).is_none());
        assert_eq!(p.placed(1, 3), None);
        let (s0, r0) = p.take(0).unwrap();
        p.put_back(0, s0, r0); // dead this frame: next frame
        let (s0, _) = p.take(0).unwrap();
        assert_eq!(p.placed(0, s0), Some((3, 0b11)));
        assert!(!p.active());
    }

    #[test]
    fn newer_reset_wins() {
        let mut p = Pending::new();
        p.offer(Reset { seq: 1, ports: [Some(pr(0.0)), None] });
        let (s, _) = p.take(0).unwrap();
        p.offer(Reset { seq: 2, ports: [Some(pr(50.0)), None] });
        assert_eq!(p.placed(0, s), None); // the old one finishing doesn't count
        let (s2, r2) = p.take(0).unwrap();
        assert_eq!((s2, r2.x), (2, 50.0));
        assert_eq!(p.placed(0, s2), Some((2, 1)));
        p.offer(Reset { seq: 4, ports: [None, Some(pr(1.0))] });
        assert_eq!(p.refuse(), Some(4));
        assert_eq!(p.refuse(), None);
    }

    #[test]
    fn jump_counts() {
        assert_eq!(jump_count_for(2, 2), 0);
        assert_eq!(jump_count_for(2, 0), 2);
        assert_eq!(jump_count_for(2, 5), 0);
        assert_eq!(jump_count_for(2, -1), 2);
    }
}
