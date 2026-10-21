//! Which scene the game is on (SSB-25). Pure logic, no skyline types, so it
//! tests on a PC; the hook that feeds it is in `menu_hook.rs`.
//!
//! The game thread calls `set` from the scene-change hook, the network thread
//! reads `snapshot` and sends it as packet kind 7 (layout in docs/protocol.md,
//! bridge/bridge/wire.py). Scene names are only known from source reading so
//! far (docs/css-research.md, experiment E1), hence the loose matching below.

use std::sync::Mutex;

use crate::net::{write_header, HEADER_LEN};

pub const KIND_SCENE: u16 = 7;
/// Scene names are cut to this many bytes.
pub const NAME_LEN: usize = 32;
/// header + changes (u32) + flags (u32) + menu_applied (u32) + menu_last_seq (u32) + name
pub const SCENE_LEN: usize = HEADER_LEN + 16 + NAME_LEN;

/// flags bit 0: the scene-change hook is installed
pub const FLAG_SCENE_HOOK: u32 = 1;
/// flags bit 1: auto_menu is on and the controller hook is installed
pub const FLAG_MENU_HOOK: u32 = 2;

/// A scene whose name contains one of these (any case) is treated as the
/// match, so menu presses are never applied there. Best guess from HDR's
/// `load_melee_scene`; E1 settles it. A name that also says "result" is the
/// results screen, where presses are exactly what we want.
/// "meleescene", not "melee": MeleeRuleScene (the rules menu) would count too
/// and block presses there (hardware 2026-10-21).
const MATCH_SCENE_HINTS: &[&str] = &["meleescene"];
const NOT_MATCH_HINTS: &[&str] = &["result"];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Scene {
    name: [u8; NAME_LEN],
    len: usize,
    /// How many scene changes have been seen since boot.
    pub changes: u32,
}

impl Scene {
    pub const fn empty() -> Scene {
        Scene { name: [0; NAME_LEN], len: 0, changes: 0 }
    }

    pub fn name(&self) -> String {
        String::from_utf8_lossy(&self.name[..self.len]).into_owned()
    }
}

static CURRENT: Mutex<Scene> = Mutex::new(Scene::empty());

/// `raw` up to its first NUL, at most NAME_LEN bytes.
fn clip(raw: &[u8]) -> &[u8] {
    let end = raw.iter().position(|&b| b == 0).unwrap_or(raw.len()).min(NAME_LEN);
    &raw[..end]
}

impl Scene {
    /// Records a scene change. Returns the new change count.
    fn apply(&mut self, raw: &[u8]) -> u32 {
        let n = clip(raw);
        self.name = [0; NAME_LEN];
        self.name[..n.len()].copy_from_slice(n);
        self.len = n.len();
        self.changes = self.changes.wrapping_add(1);
        self.changes
    }
}

/// Records a scene change. Returns the new change count.
pub fn set(raw: &[u8]) -> u32 {
    match CURRENT.lock() {
        Ok(mut s) => s.apply(raw),
        Err(p) => p.into_inner().apply(raw),
    }
}

pub fn snapshot() -> Scene {
    match CURRENT.lock() {
        Ok(s) => *s,
        Err(p) => *p.into_inner(),
    }
}

/// True when the name looks like the in-match scene.
pub fn is_match_scene(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    MATCH_SCENE_HINTS.iter().any(|h| lower.contains(h)) && !NOT_MATCH_HINTS.iter().any(|h| lower.contains(h))
}

/// Menu presses are allowed once a scene is known and it isn't the match,
/// or it is the match scene but the match is over: the victory screen shows
/// in the match scene, before UnlockSequenceScene (seen on hardware).
pub fn menu_presses_allowed() -> bool {
    let s = snapshot();
    s.len > 0 && (!is_match_scene(&s.name()) || crate::state::match_over())
}

pub fn scene_packet(seq: u32, s: &Scene, flags: u32, menu_applied: u32, menu_last_seq: u32) -> [u8; SCENE_LEN] {
    let mut buf = [0u8; SCENE_LEN];
    write_header(&mut buf, KIND_SCENE, seq);
    buf[12..16].copy_from_slice(&s.changes.to_le_bytes());
    buf[16..20].copy_from_slice(&flags.to_le_bytes());
    buf[20..24].copy_from_slice(&menu_applied.to_le_bytes());
    buf[24..28].copy_from_slice(&menu_last_seq.to_le_bytes());
    buf[28..28 + s.len].copy_from_slice(&s.name[..s.len]);
    buf
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn name_is_cut_at_nul_and_length() {
        let mut s = Scene::empty();
        assert_eq!(s.apply(b"CharaSelectScene\0garbage"), 1);
        assert_eq!(s.name(), "CharaSelectScene");
        s.apply(&[b'x'; 50]);
        assert_eq!(s.name().len(), NAME_LEN);
    }

    #[test]
    fn match_scene_detection() {
        assert!(is_match_scene("MeleeScene"));
        assert!(!is_match_scene("CharaSelectScene"));
        assert!(!is_match_scene(""));
        assert!(!is_match_scene("MeleeResultScene"));
        assert!(!is_match_scene("MeleeRuleScene"));
    }

    #[test]
    fn packet_layout() {
        let mut s = Scene::empty();
        s.apply(b"StageSelectScene");
        let b = scene_packet(5, &s, FLAG_SCENE_HOOK | FLAG_MENU_HOOK, 3, 9);
        assert_eq!(b.len(), 60);
        assert_eq!(&b[0..4], b"SCPU");
        assert_eq!(u16::from_le_bytes([b[6], b[7]]), KIND_SCENE);
        assert_eq!(u32::from_le_bytes(b[8..12].try_into().unwrap()), 5);
        assert_eq!(u32::from_le_bytes(b[12..16].try_into().unwrap()), 1);
        assert_eq!(u32::from_le_bytes(b[16..20].try_into().unwrap()), 3);
        assert_eq!(u32::from_le_bytes(b[20..24].try_into().unwrap()), 3);
        assert_eq!(u32::from_le_bytes(b[24..28].try_into().unwrap()), 9);
        assert_eq!(&b[28..44], b"StageSelectScene");
        assert!(b[44..].iter().all(|&x| x == 0));
    }
}
