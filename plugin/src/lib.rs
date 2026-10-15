//! super-cpu Skyline plugin.
//!
//! Responsibilities, in story order:
//! - SSB-1: load alongside the mod set and log on match start
//! - SSB-3: read both fighters' state every frame (see `state.rs`)
//! - SSB-4: stream frame-numbered state to the PC bridge
//! - SSB-6: apply inputs from the PC to the bot's port
//! - SSB-10: refuse to log or play on a mismatched build (see `version.rs`)

mod state;
mod version;

#[skyline::main(name = "super_cpu")]
pub fn main() {
    println!("[super-cpu] plugin loaded, expecting {}", version::EXPECTED_GAME_VERSION);

    // TODO(SSB-2): confirm hook points and accessors against the Training Modpack source.
    // TODO(SSB-3): install a per-frame hook that fills `state::FrameState` for both ports.
    // TODO(SSB-4): open a TCP connection to the bridge and send one message per frame.
    // TODO(SSB-6): apply the input message for the bot's port before the game reads controllers.
}
