//! Version guard (SSB-10). Data from a different game build or mod set
//! must never mix with the training set.
//!
//! At boot `check()` reads the version the game is actually running and
//! compares it to `EXPECTED_GAME_VERSION`. On a mismatch the plugin keeps
//! sending heartbeats (so the PC can see what's running) but no state
//! packets, so nothing from the wrong build can end up in a dataset.

use std::sync::OnceLock;

/// Set this to the Ultimate version on your Switch before collecting data.
pub const EXPECTED_GAME_VERSION: &str = "13.0.4";

/// Hash of the active mod set. Fill in once the mod list is frozen.
// Not checked yet: the mod hash half of SSB-10 is deferred until the mod
// list is frozen. Kept here so the expected value lives next to the version.
#[allow(dead_code)]
pub const EXPECTED_MOD_HASH: &str = "unset";

static RUNNING: OnceLock<String> = OnceLock::new();

/// The game's display version, e.g. "13.0.4". Read once, then cached.
///
/// Same call the Training Modpack uses for its SMASH_VERSION
/// (src/common/events.rs): fill an `oe::DisplayVersion` with
/// `oe::GetDisplayVersion` and read it as a NUL terminated string.
pub fn running_game_version() -> &'static str {
    RUNNING.get_or_init(|| {
        let mut v = skyline::nn::oe::DisplayVersion { name: [0; 16] };
        unsafe {
            skyline::nn::oe::GetDisplayVersion(&mut v);
        }
        display_name(&v.name)
    })
}

/// Bytes up to the first NUL, as text. Never panics on odd bytes.
pub fn display_name(name: &[u8; 16]) -> String {
    let end = name.iter().position(|&b| b == 0).unwrap_or(name.len());
    String::from_utf8_lossy(&name[..end]).trim().to_string()
}

pub fn version_matches(running: &str, expected: &str) -> bool {
    running.trim() == expected.trim()
}

/// True when the running game is the build we expect. Cheap after the
/// first call, safe to call every frame.
pub fn game_version_ok() -> bool {
    static OK: OnceLock<bool> = OnceLock::new();
    *OK.get_or_init(|| version_matches(running_game_version(), EXPECTED_GAME_VERSION))
}

/// Called once at boot. Logs the result either way.
pub fn check() -> bool {
    let running = running_game_version();
    let ok = game_version_ok();
    if ok {
        println!("[super-cpu] game version {running} matches, state streaming on");
    } else {
        println!(
            "[super-cpu] GAME VERSION MISMATCH: running {:?}, expected {:?}. \
             Heartbeats only, no state packets will be sent.",
            running, EXPECTED_GAME_VERSION
        );
    }
    ok
}
