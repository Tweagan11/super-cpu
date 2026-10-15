//! UDP side of the bridge. Packet layouts are in docs/protocol.md and must
//! match bridge/bridge/wire.py and bridge/bridge/protocol.py.
//!
//! Runs on its own thread. The game thread hands it finished frames through
//! a bounded channel and never waits on the network.

use std::net::UdpSocket;
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::thread::sleep;
use std::time::{Duration, Instant};

use crate::config::Config;
use crate::state::{FighterState, FrameState};
use crate::version::EXPECTED_GAME_VERSION;

pub const MAGIC: [u8; 4] = *b"SCPU";
pub const PROTO_VERSION: u16 = 1;

pub const KIND_STATE: u16 = 1;
pub const KIND_INPUT: u16 = 2;
pub const KIND_HEARTBEAT: u16 = 3;

pub const HEADER_LEN: usize = 12;
/// header + uptime_ms (u64) + game version (8 bytes, NUL padded)
pub const HEARTBEAT_LEN: usize = HEADER_LEN + 8 + 8;
pub const FIGHTER_LEN: usize = 64;
/// header + frame (u32) + stage_id (i32) + 2 fighters
pub const STATE_LEN: usize = HEADER_LEN + 8 + 2 * FIGHTER_LEN;

const HEARTBEAT_EVERY: Duration = Duration::from_millis(500);
const RETRY_AFTER: Duration = Duration::from_secs(5);

pub fn write_header(buf: &mut [u8], kind: u16, seq: u32) {
    buf[0..4].copy_from_slice(&MAGIC);
    buf[4..6].copy_from_slice(&PROTO_VERSION.to_le_bytes());
    buf[6..8].copy_from_slice(&kind.to_le_bytes());
    buf[8..12].copy_from_slice(&seq.to_le_bytes());
}

pub fn heartbeat_packet(seq: u32, uptime_ms: u64) -> [u8; HEARTBEAT_LEN] {
    let mut buf = [0u8; HEARTBEAT_LEN];
    write_header(&mut buf, KIND_HEARTBEAT, seq);
    buf[12..20].copy_from_slice(&uptime_ms.to_le_bytes());
    let v = EXPECTED_GAME_VERSION.as_bytes();
    let n = v.len().min(8);
    buf[20..20 + n].copy_from_slice(&v[..n]);
    buf
}

/// Writes one FighterState in the repr(C) layout from docs/protocol.md.
fn write_fighter(buf: &mut [u8], f: &FighterState) {
    buf[0..4].copy_from_slice(&f.fighter_kind.to_le_bytes());
    buf[4..8].copy_from_slice(&f.pos_x.to_le_bytes());
    buf[8..12].copy_from_slice(&f.pos_y.to_le_bytes());
    buf[12..16].copy_from_slice(&f.vel_x.to_le_bytes());
    buf[16..20].copy_from_slice(&f.vel_y.to_le_bytes());
    buf[20..24].copy_from_slice(&f.damage.to_le_bytes());
    buf[24..28].copy_from_slice(&f.shield_hp.to_le_bytes());
    buf[28..32].copy_from_slice(&f.stocks.to_le_bytes());
    buf[32..36].copy_from_slice(&f.status_kind.to_le_bytes());
    // 36..40 padding before the u64
    buf[40..48].copy_from_slice(&f.motion_kind.to_le_bytes());
    buf[48..52].copy_from_slice(&f.anim_frame.to_le_bytes());
    buf[52..56].copy_from_slice(&f.facing.to_le_bytes());
    buf[56..60].copy_from_slice(&f.jumps_left.to_le_bytes());
    buf[60..64].copy_from_slice(&f.flags.to_le_bytes());
}

pub fn state_packet(seq: u32, s: &FrameState) -> [u8; STATE_LEN] {
    let mut buf = [0u8; STATE_LEN];
    write_header(&mut buf, KIND_STATE, seq);
    buf[12..16].copy_from_slice(&s.frame.to_le_bytes());
    buf[16..20].copy_from_slice(&s.stage_id.to_le_bytes());
    write_fighter(&mut buf[20..20 + FIGHTER_LEN], &s.fighters[0]);
    write_fighter(&mut buf[20 + FIGHTER_LEN..STATE_LEN], &s.fighters[1]);
    buf
}

fn send(sock: &UdpSocket, packet: &[u8], target: &str) -> bool {
    match sock.send_to(packet, target) {
        Ok(_) => true,
        Err(e) => {
            println!("[super-cpu] send failed: {e}, rebinding");
            false
        }
    }
}

/// Runs forever on its own thread: forwards each finished frame as a state
/// packet and sends a heartbeat every 500 ms. Rebinds if the network drops.
pub fn run(cfg: Config, frames: Receiver<FrameState>) {
    let target = format!("{}:{}", cfg.pc_ip, cfg.udp_port);
    let start = Instant::now();
    let mut seq: u32 = 0;
    loop {
        let sock = match UdpSocket::bind("0.0.0.0:0") {
            Ok(s) => s,
            Err(e) => {
                println!("[super-cpu] udp bind failed: {e}, retrying");
                sleep(RETRY_AFTER);
                continue;
            }
        };
        println!("[super-cpu] streaming to {target}");
        let mut next_heartbeat = Instant::now();
        loop {
            let wait = next_heartbeat.saturating_duration_since(Instant::now());
            match frames.recv_timeout(wait) {
                Ok(frame) => {
                    if !send(&sock, &state_packet(seq, &frame), &target) {
                        break;
                    }
                    seq = seq.wrapping_add(1);
                }
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => return,
            }
            // Checked every pass so heartbeats keep going during matches too.
            if Instant::now() >= next_heartbeat {
                next_heartbeat = Instant::now() + HEARTBEAT_EVERY;
                let uptime_ms = start.elapsed().as_millis() as u64;
                if !send(&sock, &heartbeat_packet(seq, uptime_ms), &target) {
                    break;
                }
                seq = seq.wrapping_add(1);
            }
        }
        sleep(RETRY_AFTER);
    }
}
