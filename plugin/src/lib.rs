//! super-cpu Skyline plugin.
//!
//! Done so far:
//! - SSB-1: loads alongside the mod set, logs on boot and on match start
//! - SSB-3 first cut: reads both fighters' state every frame (`state.rs`)
//!   from a hook on the fighter's per-frame update (`frame.rs`)
//! - SSB-4 first cut: sends a state packet per frame plus heartbeats (`net.rs`)
//! - SSB-3: velocity, shield, jumps, hitstun flag, stage id
//! - SSB-6: applies inputs from the PC to the bot's port (`control.rs` hooks
//!   the game's final input mapping, `input.rs` has the timing rules)
//! - SSB-7: acks each applied input so the PC can measure round trip
//! - SSB-10 (game version half): no state packets on a mismatched build
//!   (`version.rs`)
//!
//! Still to do:
//! - SSB-3: invincible flag (no precedented accessor yet, see docs/protocol.md)
//! - SSB-10: mod set hash

mod config;
mod control;
mod frame;
mod input;
mod net;
mod offsets;
mod state;
mod version;

/// Finished frames waiting for the network thread. If the network falls this
/// far behind, new frames are dropped rather than slowing the game.
const FRAME_QUEUE: usize = 8;
/// Input acks waiting for the network thread. Same rule.
const ACK_QUEUE: usize = 16;

#[skyline::main(name = "super_cpu")]
pub fn main() {
    println!(
        "[super-cpu] plugin loaded, protocol v{}, expecting game {}",
        net::PROTO_VERSION,
        version::EXPECTED_GAME_VERSION
    );

    // On a mismatch this only logs; state.rs checks the result every frame
    // and the heartbeat carries the running version to the PC.
    version::check();

    let cfg = config::load();
    println!(
        "[super-cpu] config: pc {}:{}, bot port {} (input player_idx {})",
        cfg.pc_ip,
        cfg.udp_port,
        cfg.bot_port,
        cfg.bot_player_idx()
    );

    let (tx, rx) = std::sync::mpsc::sync_channel(FRAME_QUEUE);
    let (ack_tx, ack_rx) = std::sync::mpsc::sync_channel(ACK_QUEUE);
    state::init(tx);
    input::init(cfg.bot_port, cfg.bot_player_idx(), ack_tx);
    frame::install();
    control::install(cfg.cpu_probe);

    // Networking never runs on the game thread.
    std::thread::spawn(move || net::run(cfg, rx, ack_rx, health));
}

fn health() -> net::Health {
    use std::sync::atomic::Ordering::Relaxed;
    net::Health {
        hook_installed: frame::HOOK_INSTALLED.load(Relaxed),
        fighter_calls: frame::FIGHTER_CALLS.load(Relaxed),
        frames_queued: state::FRAMES_QUEUED.load(Relaxed),
    }
}
