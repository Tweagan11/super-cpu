//! UDP side of the bridge. Packet layouts are in docs/protocol.md and must
//! match bridge/bridge/wire.py.

use std::net::UdpSocket;
use std::thread::sleep;
use std::time::{Duration, Instant};

use crate::config::Config;
use crate::version::EXPECTED_GAME_VERSION;

pub const MAGIC: [u8; 4] = *b"SCPU";
pub const PROTO_VERSION: u16 = 1;

pub const KIND_STATE: u16 = 1;
pub const KIND_INPUT: u16 = 2;
pub const KIND_HEARTBEAT: u16 = 3;

pub const HEADER_LEN: usize = 12;
/// header + uptime_ms (u64) + game version (8 bytes, NUL padded)
pub const HEARTBEAT_LEN: usize = HEADER_LEN + 8 + 8;

const HEARTBEAT_EVERY: Duration = Duration::from_millis(500);
const RETRY_AFTER: Duration = Duration::from_secs(5);

pub fn write_header(buf: &mut [u8], kind: u16, seq: u32) {
    buf[0..4].copy_from_slice(&MAGIC);
    buf[4..6].copy_from_slice(&PROTO_VERSION.to_le_bytes());
    buf[6..8].copy_from_slice(&kind.to_le_bytes());
    buf[8..12].copy_from_slice(&seq.to_le_bytes());
}

fn heartbeat_packet(seq: u32, uptime_ms: u64) -> [u8; HEARTBEAT_LEN] {
    let mut buf = [0u8; HEARTBEAT_LEN];
    write_header(&mut buf, KIND_HEARTBEAT, seq);
    buf[12..20].copy_from_slice(&uptime_ms.to_le_bytes());
    let v = EXPECTED_GAME_VERSION.as_bytes();
    let n = v.len().min(8);
    buf[20..20 + n].copy_from_slice(&v[..n]);
    buf
}

/// Runs forever on its own thread. Sends a heartbeat every 500 ms so the PC
/// can see the plugin is alive. If the network drops, it waits and rebinds.
pub fn heartbeat_loop(cfg: Config) {
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
        println!("[super-cpu] sending heartbeats to {target}");
        loop {
            let uptime_ms = start.elapsed().as_millis() as u64;
            if let Err(e) = sock.send_to(&heartbeat_packet(seq, uptime_ms), &target) {
                println!("[super-cpu] send failed: {e}, rebinding");
                sleep(RETRY_AFTER);
                break;
            }
            seq = seq.wrapping_add(1);
            sleep(HEARTBEAT_EVERY);
        }
    }
}
