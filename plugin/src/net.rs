//! UDP side of the bridge. Packet layouts are in docs/protocol.md and must
//! match bridge/bridge/wire.py and bridge/bridge/protocol.py.
//!
//! Runs on its own thread. The game thread hands it finished frames (and
//! input acks) through bounded channels and never waits on the network.
//! Input packets (kind 2) come back on the same socket and go to `input.rs`.

use std::io::ErrorKind;
use std::net::{IpAddr, UdpSocket};
use std::sync::atomic::Ordering;
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::thread::sleep;
use std::time::{Duration, Instant};

use crate::config::Config;
use crate::input::{self, ack_packet, status_packet, Ack, InputReceiver, Inputs};
use crate::state::{FighterState, FrameState};
use crate::version::running_game_version;

pub const MAGIC: [u8; 4] = *b"SCPU";
/// v2: heartbeat carries hook health (hook_installed, fighter_calls, frames_queued)
/// Kinds 2, 4 and 5 were added without a bump: nothing older sends or
/// expects them, and the heartbeat and state layouts didn't change.
/// v3: state packet carries both ports' captured inputs (SSB-13), control
/// status byte 15 is the other port's player_idx.
pub const PROTO_VERSION: u16 = 3;

pub const KIND_STATE: u16 = 1;
pub const KIND_INPUT: u16 = 2;
pub const KIND_HEARTBEAT: u16 = 3;
// KIND_ACK (4) and KIND_CONTROL_STATUS (5) live in input.rs.

pub const HEADER_LEN: usize = 12;
/// header + uptime_ms (u64) + game version (8 bytes, NUL padded)
/// + hook_installed (u32) + fighter_calls (u32) + frames_queued (u32)
pub const HEARTBEAT_LEN: usize = HEADER_LEN + 8 + 8 + 12;
pub const FIGHTER_LEN: usize = 64;
/// One port's captured inputs: 4 x i8 sticks + u32 buttons.
pub const INPUTS_LEN: usize = 8;
/// header + frame (u32) + stage_id (i32) + 2 fighters + input_flags (u32)
/// + 2 captured inputs
pub const STATE_LEN: usize = HEADER_LEN + 8 + 2 * FIGHTER_LEN + 4 + 2 * INPUTS_LEN;

const HEARTBEAT_EVERY: Duration = Duration::from_millis(500);
const RETRY_AFTER: Duration = Duration::from_secs(5);

pub fn write_header(buf: &mut [u8], kind: u16, seq: u32) {
    buf[0..4].copy_from_slice(&MAGIC);
    buf[4..6].copy_from_slice(&PROTO_VERSION.to_le_bytes());
    buf[6..8].copy_from_slice(&kind.to_le_bytes());
    buf[8..12].copy_from_slice(&seq.to_le_bytes());
}

/// Hook health sent with every heartbeat.
#[derive(Clone, Copy, Default, Debug)]
pub struct Health {
    pub hook_installed: bool,
    pub fighter_calls: u32,
    pub frames_queued: u32,
}

pub fn heartbeat_packet(seq: u32, uptime_ms: u64, h: Health) -> [u8; HEARTBEAT_LEN] {
    let mut buf = [0u8; HEARTBEAT_LEN];
    write_header(&mut buf, KIND_HEARTBEAT, seq);
    buf[12..20].copy_from_slice(&uptime_ms.to_le_bytes());
    // The version the game is really running (SSB-10), so the PC's
    // mismatch warning reports what's on the Switch, not what we expected.
    let v = running_game_version().as_bytes();
    let n = v.len().min(8);
    buf[20..20 + n].copy_from_slice(&v[..n]);
    buf[28..32].copy_from_slice(&(h.hook_installed as u32).to_le_bytes());
    buf[32..36].copy_from_slice(&h.fighter_calls.to_le_bytes());
    buf[36..40].copy_from_slice(&h.frames_queued.to_le_bytes());
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
    write_fighter(&mut buf[20 + FIGHTER_LEN..20 + 2 * FIGHTER_LEN], &s.fighters[1]);
    let at = 20 + 2 * FIGHTER_LEN;
    buf[at..at + 4].copy_from_slice(&s.input_flags.to_le_bytes());
    write_inputs(&mut buf[at + 4..at + 4 + INPUTS_LEN], &s.inputs[0]);
    write_inputs(&mut buf[at + 4 + INPUTS_LEN..STATE_LEN], &s.inputs[1]);
    buf
}

/// Same order as the input packet and the game's MappedInputs: sticks, then buttons.
fn write_inputs(buf: &mut [u8], i: &Inputs) {
    buf[0] = i.stick_x as u8;
    buf[1] = i.stick_y as u8;
    buf[2] = i.cstick_x as u8;
    buf[3] = i.cstick_y as u8;
    buf[4..8].copy_from_slice(&i.buttons.to_le_bytes());
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

/// Reads every input packet waiting on the socket (it's non-blocking) and
/// hands each to the receiver. Returns false on an unexpected receive error;
/// the caller then stops receiving on this socket but keeps sending, so a
/// receive problem can never interrupt state. Only packets from the
/// configured PC are accepted.
fn drain_inputs(sock: &UdpSocket, rx: &mut InputReceiver, pc_ip: Option<IpAddr>, live_frame: u32) -> bool {
    let mut buf = [0u8; 64];
    // Bounded so a flood can't starve the send side.
    for _ in 0..MAX_DRAIN {
        match sock.recv_from(&mut buf) {
            Ok((n, from)) => {
                if pc_ip.map_or(true, |ip| ip == from.ip()) {
                    rx.handle(&input::SHARED, &buf[..n], input::now_ms(), live_frame);
                } else {
                    input::SHARED.rejected.fetch_add(1, Ordering::Relaxed);
                }
            }
            Err(e) if e.kind() == ErrorKind::WouldBlock || e.kind() == ErrorKind::TimedOut => return true,
            // Windows-style "port unreachable" bounces off a closed bridge; harmless.
            Err(e) if e.kind() == ErrorKind::ConnectionReset || e.kind() == ErrorKind::ConnectionRefused => continue,
            Err(e) => {
                println!("[super-cpu] udp receive failed: {e}, inputs off until the socket is rebound");
                return false;
            }
        }
    }
    true
}

const MAX_DRAIN: usize = 32;

/// How long the loop waits on the frame channel before checking the socket
/// again. Bounds how stale an input can get before it's filed (SSB-6).
const POLL: Duration = Duration::from_millis(1);

/// Makes `sock` safe to receive on from the send loop. True if it is.
fn setup_receive(sock: &UdpSocket) -> bool {
    // Set both: non-blocking is what we want, the 1 ms read timeout is the
    // backstop in case one is accepted but ignored.
    let nonblocking = sock.set_nonblocking(true).is_ok();
    let timeout = sock.set_read_timeout(Some(POLL)).is_ok();
    nonblocking || timeout
}

/// One-time check that an empty receive really comes back right away. If
/// the socket layer accepted our settings but ignored them, `recv_from`
/// would block until a packet arrives, and since the PC only sends inputs
/// in answer to state, state would stop for good. So test it on a throwaway
/// socket in a throwaway thread first. If the test hangs, that thread and
/// socket just sit there unused, and inputs stay off.
fn receive_works() -> bool {
    let (tx, rx) = std::sync::mpsc::channel();
    let spawned = std::thread::Builder::new().spawn(move || {
        let ok = match UdpSocket::bind("0.0.0.0:0") {
            Ok(s) if setup_receive(&s) => {
                let mut buf = [0u8; 8];
                let t = Instant::now();
                matches!(s.recv_from(&mut buf), Err(_)) && t.elapsed() < Duration::from_millis(100)
            }
            _ => false,
        };
        let _ = tx.send(ok);
    });
    spawned.is_ok() && rx.recv_timeout(Duration::from_millis(500)).unwrap_or(false)
}

/// Runs forever on its own thread: forwards each finished frame as a state
/// packet, sends a heartbeat (plus a control status packet) every 500 ms,
/// receives input packets on the same socket so the PC can just reply to
/// where state came from, and sends an ack for each input the game applied.
/// Rebinds if the network drops. `health` is read fresh for each heartbeat.
pub fn run(cfg: Config, frames: Receiver<FrameState>, acks: Receiver<Ack>, health: fn() -> Health) {
    let target = format!("{}:{}", cfg.pc_ip, cfg.udp_port);
    let pc_ip: Option<IpAddr> = cfg.pc_ip.parse().ok();
    let start = Instant::now();
    let mut seq: u32 = 0;
    let mut rx = InputReceiver::new();
    let mut live_frame: u32 = 0;
    let can_receive = receive_works();
    if !can_receive {
        println!("[super-cpu] non-blocking UDP receive doesn't work here, inputs disabled");
    }
    loop {
        let sock = match UdpSocket::bind("0.0.0.0:0") {
            Ok(s) => s,
            Err(e) => {
                println!("[super-cpu] udp bind failed: {e}, retrying");
                sleep(RETRY_AFTER);
                continue;
            }
        };
        // Never let a receive stall the send loop. If that can't be
        // guaranteed, inputs are off but state keeps flowing.
        let mut receiving = can_receive && setup_receive(&sock);
        input::RECEIVING.store(receiving, Ordering::Relaxed);
        if can_receive && !receiving {
            println!("[super-cpu] couldn't make the socket non-blocking, inputs disabled");
        }
        println!("[super-cpu] streaming to {target}");
        let mut next_heartbeat = Instant::now();
        'conn: loop {
            if receiving && !drain_inputs(&sock, &mut rx, pc_ip, live_frame) {
                receiving = false;
                input::RECEIVING.store(false, Ordering::Relaxed);
            }
            while let Ok(a) = acks.try_recv() {
                if !send(&sock, &ack_packet(seq, &a), &target) {
                    break 'conn;
                }
                seq = seq.wrapping_add(1);
            }
            let wait = next_heartbeat.saturating_duration_since(Instant::now());
            let wait = if receiving { wait.min(POLL) } else { wait };
            match frames.recv_timeout(wait) {
                Ok(frame) => {
                    live_frame = frame.frame;
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
                if !send(&sock, &heartbeat_packet(seq, uptime_ms, health()), &target) {
                    break;
                }
                seq = seq.wrapping_add(1);
                if !send(&sock, &status_packet(seq, &input::status()), &target) {
                    break;
                }
                seq = seq.wrapping_add(1);
            }
        }
        input::RECEIVING.store(false, Ordering::Relaxed);
        sleep(RETRY_AFTER);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hex(b: &[u8]) -> String {
        b.iter().map(|x| format!("{x:02x}")).collect()
    }

    /// Same values as golden_state() in bridge/tests/test_capture.py.
    pub fn golden_frame() -> FrameState {
        let f = |dmg: f32, side: f32| FighterState {
            fighter_kind: 0x2D, pos_x: 12.5 * side, pos_y: 3.25, vel_x: -1.5 * side, vel_y: 2.75,
            damage: dmg, shield_hp: 37.5, stocks: 3, status_kind: 0x47, motion_kind: 0xABCDEF0123,
            anim_frame: 4.0, facing: -1, jumps_left: 1, flags: 0b11,
        };
        FrameState {
            frame: 1234,
            stage_id: 1,
            fighters: [f(42.5, 1.0), f(10.0, -1.0)],
            inputs: [
                Inputs { stick_x: 127, stick_y: -128, cstick_x: 0, cstick_y: 40, buttons: 0x5 },
                Inputs { stick_x: -64, stick_y: 0, cstick_x: 0, cstick_y: 0, buttons: 0x10008 },
            ],
            input_flags: input::CAP_P1_VALID | input::CAP_P2_VALID | input::CAP_P2_DRIVEN,
        }
    }

    #[test]
    fn state_v3_golden() {
        assert_eq!(STATE_LEN, 168);
        let b = state_packet(9, &golden_frame());
        // the v2 part is unchanged apart from the version, so the old
        // golden (bridge/tests/test_fields.py) is a prefix of this one
        assert_eq!(&hex(&b[..4]), "53435055");
        assert_eq!(&hex(&b[4..6]), "0300");
        assert_eq!(&hex(&b[148..]), "0b0000007f80002805000000c000000008000100");
        println!("STATE_V3_HEX {}", hex(&b));
    }
}
