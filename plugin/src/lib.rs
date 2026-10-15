//! super-cpu Skyline plugin.
//!
//! Done so far:
//! - SSB-1: loads alongside the mod set and logs on boot
//! - first slice of SSB-4: heartbeat packets to the PC over UDP (see `net.rs`)
//!
//! Still to do, in story order:
//! - SSB-2: confirm hook points against the Training Modpack source
//! - SSB-3: read both fighters' state every frame (see `state.rs`)
//! - SSB-4: send a state packet per frame alongside the heartbeat
//! - SSB-6: apply inputs from the PC to the bot's port
//! - SSB-10: refuse to log or play on a mismatched build (see `version.rs`)

mod config;
mod net;
mod state;
mod version;

#[skyline::main(name = "super_cpu")]
pub fn main() {
    println!(
        "[super-cpu] plugin loaded, protocol v{}, expecting game {}",
        net::PROTO_VERSION,
        version::EXPECTED_GAME_VERSION
    );

    let cfg = config::load();
    println!("[super-cpu] config: pc {}:{}", cfg.pc_ip, cfg.udp_port);

    // Networking never runs on the game thread.
    std::thread::spawn(move || net::heartbeat_loop(cfg));
}
