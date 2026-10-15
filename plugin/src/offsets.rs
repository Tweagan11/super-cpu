//! Game function offsets found by byte-pattern search, so they survive small
//! game updates and nothing is a hardcoded address.
//!
//! Approach and needles copied from the Training Modpack,
//! `src/common/offsets.rs` (`byte_search`, `find_offset`, `impl_offset!`),
//! which in turn got `byte_search` from HDR and ARCropolis. Differences from
//! the modpack: a missing or ambiguous needle returns None instead of
//! panicking, so a game update just disables the feature that needs it
//! instead of crashing the game at boot.
//!
//! To add one: define a NEEDLE_ (first 8 instructions, 32 bytes, is the
//! modpack's rule of thumb) and an `impl_offset!` line.

use std::sync::OnceLock;

/// Pure part of the search, split out so it's testable off the Switch.
/// Ok(offset) only if the needle shows up exactly once.
pub fn find_unique(haystack: &[u8], needle: &[u8]) -> Result<usize, &'static str> {
    use memchr::memmem;
    let first = memmem::find(haystack, needle);
    let last = memmem::rfind(haystack, needle);
    match (first, last) {
        (Some(a), Some(b)) if a == b => Ok(a),
        (Some(_), Some(_)) => Err("found more than once, extend the needle"),
        _ => Err("not found"),
    }
}

/// Searches the main executable's .text (the modpack searches from the start
/// of .text to the start of .rodata, same here).
pub fn byte_search(needle: &[u8]) -> Result<usize, &'static str> {
    let haystack = unsafe {
        let start = skyline::hooks::getRegionAddress(skyline::hooks::Region::Text) as *const u8;
        let end = skyline::hooks::getRegionAddress(skyline::hooks::Region::Rodata) as *const u8;
        let length = end.offset_from(start) as usize;
        std::slice::from_raw_parts(start, length)
    };
    find_unique(haystack, needle)
}

fn find_offset(name: &str, needle: &[u8]) -> Option<usize> {
    match byte_search(needle) {
        Ok(off) => {
            println!("[super-cpu] found {name} at text+{off:#x}");
            Some(off)
        }
        Err(why) => {
            println!("[super-cpu] offset {name} {why}");
            None
        }
    }
}

/// `impl_offset!(NAME, NEEDLE, getter)` gives `getter() -> Option<usize>`,
/// searched once on first use. (The modpack builds the names with `paste`;
/// spelling them out keeps the dependency list short.)
macro_rules! impl_offset {
    ($name:ident, $needle:ident, $getter:ident) => {
        pub fn $getter() -> Option<usize> {
            static CELL: OnceLock<Option<usize>> = OnceLock::new();
            *CELL.get_or_init(|| find_offset(stringify!($name), $needle))
        }
    };
}

// Final input mapping: turns one controller's raw buttons and sticks into the
// MappedInputs the fighter reads. Called once per player per frame.
// From the Training Modpack, src/common/offsets.rs:
// OFFSET_FIM = 0x1750dd0 (that's where it was in the build they wrote the
// comment for; the search finds it wherever it is now). The 7th instruction
// is `cmn w1, #1`, i.e. "player_idx == -1", which matches the hook's second
// argument being player_idx.
static NEEDLE_FIM: &[u8] = &[
    0xff, 0x03, 0x02, 0xd1,
    0xf7, 0x23, 0x00, 0xf9,
    0xf6, 0x57, 0x05, 0xa9,
    0xf4, 0x4f, 0x06, 0xa9,
    0xfd, 0x7b, 0x07, 0xa9,
    0xfd, 0xc3, 0x01, 0x91,
    0x3f, 0x04, 0x00, 0x31,
    0xe0, 0x77, 0x00, 0x54,
];
impl_offset!(FIM, NEEDLE_FIM, fim);

// Runs right after the CPU AI writes its controls. We only read it for now
// (control.rs's probe) to learn what `controller_index` means on hardware,
// which decides whether the bot can take over a CPU slot later.
// From the Training Modpack, src/common/offsets.rs:
// OFFSET_SET_CPU_CONTROLS = 0x2da180. Hooked in src/training/input_record.rs.
static NEEDLE_SET_CPU_CONTROLS: &[u8] = &[
    0xff, 0x03, 0x06, 0xd1,
    0xee, 0x73, 0x00, 0xfd,
    0xed, 0x33, 0x0f, 0x6d,
    0xeb, 0x2b, 0x10, 0x6d,
    0xe9, 0x23, 0x11, 0x6d,
    0xfc, 0x6f, 0x12, 0xa9,
    0xfa, 0x67, 0x13, 0xa9,
    0xf8, 0x5f, 0x14, 0xa9,
];
impl_offset!(SET_CPU_CONTROLS, NEEDLE_SET_CPU_CONTROLS, set_cpu_controls);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unique_needle() {
        let mut text = vec![0u8; 4096];
        text[1000..1032].copy_from_slice(NEEDLE_FIM);
        assert_eq!(find_unique(&text, NEEDLE_FIM), Ok(1000));
        text[3000..3032].copy_from_slice(NEEDLE_FIM);
        assert!(find_unique(&text, NEEDLE_FIM).is_err());
        assert!(find_unique(&[0u8; 64], NEEDLE_FIM).is_err());
    }
}
