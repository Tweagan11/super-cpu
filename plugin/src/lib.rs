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
//! - SSB-13: captures every port's final mapped inputs and sends them with
//!   the state packet (protocol v3), so human play can be logged for
//!   imitation learning
//! - SSB-25: reports the current scene (packet kind 7) and, with
//!   `auto_menu = true`, presses menu buttons for the PC (kind 8)
//!   (`scene.rs`, `menu.rs`, `menu_hook.rs`)
//! - SSB-24: with `force_fighter = true`, forces green R.O.B. onto the
//!   configured ports by writing the game's decided-fighter record
//!   (`force.rs`, `force_hook.rs`), and checks it in the match
//! - drive_cpu: with `drive_cpu = true` the PC drives a CPU slot instead of a
//!   controller, written in the CPU controls hook (`control.rs`, `cpu_drive.rs`)
//! - SSB-10 (game version half): no state packets on a mismatched build
//!   (`version.rs`)
//!
//! Still to do:
//! - SSB-3: invincible flag (no precedented accessor yet, see docs/protocol.md)
//! - SSB-10: mod set hash

mod config;
mod control;
mod cpu_drive;
mod drill;
mod drill_apply;
mod force;
mod force_hook;
mod frame;
mod input;
mod log;
mod menu;
mod menu_hook;
mod net;
mod offsets;
mod scene;
mod state;
mod version;

/// Finished frames waiting for the network thread. If the network falls this
/// far behind, new frames are dropped rather than slowing the game.
const FRAME_QUEUE: usize = 8;
/// Input acks waiting for the network thread. Same rule.
const ACK_QUEUE: usize = 16;

#[skyline::main(name = "super_cpu")]
pub fn main() {
    crate::log::log(format!("plugin loaded, protocol v{}, expecting game {}",
        net::PROTO_VERSION,
        version::EXPECTED_GAME_VERSION
    ));

    // On a mismatch this only logs; state.rs checks the result every frame
    // and the heartbeat carries the running version to the PC.
    version::check();

    let cfg = config::load();
    crate::log::log(format!("config: pc {}:{}, bot port {} (input player_idx {})",
        cfg.pc_ip,
        cfg.udp_port,
        cfg.bot_port,
        cfg.bot_player_idx()
    ));

    let (tx, rx) = std::sync::mpsc::sync_channel(FRAME_QUEUE);
    let (ack_tx, ack_rx) = std::sync::mpsc::sync_channel(ACK_QUEUE);
    state::init(tx);
    input::init(cfg.bot_port, cfg.bot_player_idx(), cfg.fim_player_idx.is_some(), ack_tx);
    frame::install();
    control::install(cfg.cpu_probe, cfg.drive_cpu, cfg.cpu_controller_index);
    menu_hook::install(cfg.menu_npad, cfg.auto_menu);
    force_hook::install(cfg.force_fighter, &cfg.force_ports, cfg.force_costume, cfg.force_copy_hook);
    drill_apply::install(cfg.drills, cfg.drills_in_versus);

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
