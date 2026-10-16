//! Forced fighter choice (SSB-24). Pure logic, no skyline types, so it tests
//! on a PC; the hooks that feed it are in `force_hook.rs`.
//!
//! The game keeps a per-player "decided fighter" record. For a forced port we
//! overwrite exactly two fields of it: the ui_chara hash (low 40 bits of the
//! 64-bit word at `+0x18`, the top 24 bits are kept) and the costume byte at
//! `+0x20`. Layout and offsets are from docs/css-research.md (HDR-1304
//! `chara_select/random.rs`), used as facts only.
//!
//! This module decides what to write, rate limits the log, and compares what
//! the match actually looks like with what was forced ("force check").

use std::sync::Mutex;

/// hash40("ui_chara_robot"): length 14 in the top byte, crc32 below.
pub const ROB_HASH: u64 = 0x0e_1885_7219;
/// The record's word holds flags above bit 40; they are kept.
pub const KEY_MASK: u64 = 0xFFFFFF_0000000000;
/// `fighter_kind` of R.O.B. (FIGHTER_KIND_ROBOT).
pub const ROB_FIGHTER_KIND: i32 = 0x2D;
/// Bytes hex-dumped from the start of the record.
pub const DUMP_LEN: usize = 0x30;
/// Which u64 word of the record gets the hash (doc: `base[3]`).
pub const HASH_WORD: usize = 3;
/// Byte offset of the costume.
pub const COSTUME_OFFSET: usize = 0x20;

/// Every fire of a port is logged this many times, then only changes.
const LOG_ALWAYS: u32 = 12;
/// Hex dumps of the record, per phase.
const DUMPS: u32 = 4;

pub const MAX_PORTS: usize = 8;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Plan {
    pub hash_word: u64,
    pub costume: u8,
    /// Anything differs from what the game had.
    pub changes: bool,
}

#[derive(Clone, Copy)]
struct PortLog {
    fires: u32,
    last: Option<(u64, u8, u8)>,
    /// The hook fired for this port since the last in-match check.
    fired: bool,
}

struct State {
    ports: [bool; MAX_PORTS],
    costume: u8,
    log: [PortLog; MAX_PORTS],
    dumps: [u32; 3],
}

static STATE: Mutex<State> = Mutex::new(State {
    ports: [false; MAX_PORTS],
    costume: 5,
    log: [PortLog { fires: 0, last: None, fired: false }; MAX_PORTS],
    dumps: [0; 3],
});

fn st() -> std::sync::MutexGuard<'static, State> {
    STATE.lock().unwrap_or_else(|p| p.into_inner())
}

/// `ports` are 1-based.
pub fn init(ports: &[u8], costume: u8) {
    let mut s = st();
    s.ports = [false; MAX_PORTS];
    for &p in ports {
        if (1..=MAX_PORTS as u8).contains(&p) {
            s.ports[p as usize - 1] = true;
        }
    }
    s.costume = costume;
}

pub fn is_forced(port: u8) -> bool {
    (1..=MAX_PORTS as u8).contains(&port) && st().ports[port as usize - 1]
}

pub fn costume() -> u8 {
    st().costume
}

/// What to write into a record whose hash word is `word` and costume byte is
/// `costume`, given the forced costume `want`. Only the low 40 bits of the
/// word change.
pub fn plan(word: u64, costume: u8, want: u8) -> Plan {
    let hash_word = ROB_HASH | (word & KEY_MASK);
    Plan { hash_word, costume: want, changes: hash_word != word || costume != want }
}

/// Whether this fire gets a log line. Also marks the port as fired.
/// `key` is what the hook saw (hash, costume, phase).
pub fn should_log(port: u8, key: (u64, u8, u8)) -> bool {
    let mut s = st();
    let Some(p) = s.log.get_mut((port as usize).wrapping_sub(1)) else { return false };
    p.fires = p.fires.saturating_add(1);
    p.fired = true;
    let changed = p.last != Some(key);
    p.last = Some(key);
    p.fires <= LOG_ALWAYS || changed
}

/// True for the first few times per phase (0 pre, 1 post, 2 copy).
pub fn should_dump(phase: usize) -> bool {
    let mut s = st();
    match s.dumps.get_mut(phase) {
        Some(n) if *n < DUMPS => {
            *n += 1;
            true
        }
        _ => false,
    }
}

/// Hex bytes in groups of 8.
pub fn hex_dump(bytes: &[u8]) -> String {
    let mut out = String::new();
    for (i, b) in bytes.iter().enumerate() {
        if i > 0 && i % 8 == 0 {
            out.push(' ');
        }
        out.push_str(&format!("{b:02x}"));
    }
    out
}

/// Compares the match with what was forced. Call once per fighter at match
/// start with its 1-based port, `fighter_kind` and costume. None when the
/// port isn't forced. Clears the port's fired flag, so each match needs a
/// fresh fire of the hook to count as forced.
pub fn check(port: u8, kind: i32, color: i32) -> Option<String> {
    let mut s = st();
    let idx = (port as usize).wrapping_sub(1);
    if idx >= MAX_PORTS || !s.ports[idx] {
        return None;
    }
    let want = s.costume;
    let fired = std::mem::take(&mut s.log[idx].fired);
    let name = |k: i32| if k == ROB_FIGHTER_KIND { "robot".to_string() } else { format!("kind {k}") };
    Some(if !fired {
        format!("force check: P{port} NOT FORCED, hook never fired for it before this match; got {} c{color:02}", name(kind))
    } else if kind == ROB_FIGHTER_KIND && color == want as i32 {
        format!("force check: P{port} robot c{want:02} OK")
    } else {
        format!("force check: P{port} MISMATCH want robot c{want:02}, got {} c{color:02}", name(kind))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rob_hash_is_length_and_crc() {
        assert_eq!(ROB_HASH >> 32, "ui_chara_robot".len() as u64);
        assert_eq!(ROB_HASH & KEY_MASK, 0);
    }

    #[test]
    fn plan_keeps_the_flag_bits() {
        let word = 0xC1_0003_u64 << 40 | 0x12_3456_789a;
        let p = plan(word, 2, 5);
        assert_eq!(p.hash_word & KEY_MASK, word & KEY_MASK);
        assert_eq!(p.hash_word & !KEY_MASK, ROB_HASH);
        assert_eq!(p.costume, 5);
        assert!(p.changes);
    }

    #[test]
    fn plan_is_idempotent() {
        let p = plan(0xC1 << 56 | 0x1234, 0, 5);
        let again = plan(p.hash_word, p.costume, 5);
        assert!(!again.changes);
        assert_eq!(again.hash_word, p.hash_word);
        assert!(plan(ROB_HASH, 4, 5).changes, "costume alone counts");
    }

    #[test]
    fn dump_format() {
        let b: Vec<u8> = (0..17).collect();
        assert_eq!(hex_dump(&b), "0001020304050607 08090a0b0c0d0e0f 10");
        assert_eq!(hex_dump(&[]), "");
        assert_eq!(hex_dump(&[0u8; DUMP_LEN]).len(), DUMP_LEN * 2 + DUMP_LEN / 8 - 1);
    }

    // one test touches the shared state, so the parallel runner can't race it
    #[test]
    fn ports_logging_and_check() {
        init(&[2, 9, 0], 5);
        assert!(is_forced(2) && !is_forced(1) && !is_forced(9) && !is_forced(0));
        assert_eq!(costume(), 5);
        assert!(check(1, 3, 0).is_none(), "unforced port has no check");
        assert!(check(2, 3, 0).unwrap().contains("NOT FORCED"));

        let key = (ROB_HASH, 5, 0);
        for _ in 0..LOG_ALWAYS {
            assert!(should_log(2, key));
        }
        assert!(!should_log(2, key), "repeats go quiet after the first few");
        assert!(should_log(2, (ROB_HASH, 4, 0)), "a change is logged");
        assert!(!should_log(0, key) && !should_log(200, key));

        assert_eq!(check(2, ROB_FIGHTER_KIND, 5).unwrap(), "force check: P2 robot c05 OK");
        assert!(check(2, ROB_FIGHTER_KIND, 5).unwrap().contains("NOT FORCED"), "fired flag was consumed");
        should_log(2, key);
        let m = check(2, 12, 3).unwrap();
        assert!(m.contains("MISMATCH") && m.contains("kind 12 c03"), "{m}");
        should_log(2, key);
        assert!(check(2, ROB_FIGHTER_KIND, 4).unwrap().contains("MISMATCH"));

        for _ in 0..DUMPS {
            assert!(should_dump(0));
        }
        assert!(!should_dump(0) && should_dump(1) && !should_dump(7));
    }
}
