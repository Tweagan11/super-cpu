//! Plugin log lines sent to the PC (packet kind 6), because `cargo skyline
//! listen` doesn't show our println output on every setup. Each line gets an
//! id counting up from 0 since boot; the PC prints ids it hasn't seen.
//! Layout: header + id (u32) + len (u16) + UTF-8 text (at most MAX_TEXT bytes).

use std::collections::VecDeque;
use std::sync::Mutex;

use crate::net::{write_header, HEADER_LEN};

pub const KIND_LOG: u16 = 6;
pub const MAX_TEXT: usize = 200;
/// How many recent lines are kept and resent.
const KEEP: usize = 24;

static LINES: Mutex<VecDeque<(u32, String)>> = Mutex::new(VecDeque::new());
static NEXT_ID: Mutex<u32> = Mutex::new(0);

/// println! it and queue it for the PC.
pub fn log(text: impl Into<String>) {
    let mut text = text.into();
    println!("[super-cpu] {text}");
    if text.len() > MAX_TEXT {
        let mut cut = MAX_TEXT;
        while !text.is_char_boundary(cut) {
            cut -= 1;
        }
        text.truncate(cut);
    }
    let (Ok(mut lines), Ok(mut next)) = (LINES.lock(), NEXT_ID.lock()) else { return };
    lines.push_back((*next, text));
    *next += 1;
    while lines.len() > KEEP {
        lines.pop_front();
    }
}

/// Lines with id >= `from`, oldest first.
pub fn pending(from: u32) -> Vec<(u32, String)> {
    match LINES.lock() {
        Ok(lines) => lines.iter().filter(|(id, _)| *id >= from).cloned().collect(),
        Err(_) => Vec::new(),
    }
}

pub fn log_packet(seq: u32, id: u32, text: &str) -> Vec<u8> {
    let t = &text.as_bytes()[..text.len().min(MAX_TEXT)];
    let mut buf = vec![0u8; HEADER_LEN + 6 + t.len()];
    write_header(&mut buf, KIND_LOG, seq);
    buf[HEADER_LEN..HEADER_LEN + 4].copy_from_slice(&id.to_le_bytes());
    buf[HEADER_LEN + 4..HEADER_LEN + 6].copy_from_slice(&(t.len() as u16).to_le_bytes());
    buf[HEADER_LEN + 6..].copy_from_slice(t);
    buf
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn packet_layout() {
        let b = log_packet(7, 3, "hi");
        assert_eq!(&b[0..4], b"SCPU");
        assert_eq!(u16::from_le_bytes([b[6], b[7]]), KIND_LOG);
        assert_eq!(u32::from_le_bytes(b[12..16].try_into().unwrap()), 3);
        assert_eq!(u16::from_le_bytes([b[16], b[17]]), 2);
        assert_eq!(&b[18..], b"hi");
    }

    #[test]
    fn long_lines_are_cut_on_a_char_boundary() {
        log("é".repeat(150));
        let (_, t) = pending(0).pop().unwrap();
        assert!(t.len() <= MAX_TEXT && t.chars().all(|c| c == 'é'));
    }
}
