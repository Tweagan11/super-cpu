//! UDP side of the bridge. Packet layouts are in docs/protocol.md and must
//! match bridge/bridge/wire.py and bridge/bridge/protocol.py.
//!
//! Runs on its own thread. The game thread hands it finished frames (and
//! input acks) through bounded channels and never waits on the network.
//! Input packets (kind 2) come back on the same socket (shared with a receive
//! thread), and go to `input.rs`. Log lines go out as kind 6.

use std::io::ErrorKind;
use std::net::{IpAddr, UdpSocket};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::Arc;
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::thread::sleep;
use std::time::{Duration, Instant};

use crate::config::Config;
use crate::log;
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

/// The state frame the game is on, for filing inputs. Written by the send
/// loop, read by the receive thread.
static LIVE_FRAME: AtomicU32 = AtomicU32::new(0);
/// Set once any packet from the PC arrives. After that, log lines are no
/// longer resent in bursts (a running bridge has already seen them).
static PC_SEEN: AtomicBool = AtomicBool::new(false);
/// Bumped on every rebind so an old receive thread knows to quit.
static SOCKET_GEN: AtomicU32 = AtomicU32::new(0);

/// Receives input packets on their own thread, on the same socket the send
/// loop uses (shared through an Arc), so the PC can just reply to where state
/// came from. The Switch can't duplicate sockets (`try_clone` fails with
/// os error 1), but one thread blocking in recv while another sends on the
/// same socket is fine. The read timeout bounds how long a receive waits, so
/// the thread notices a rebind, and so a socket layer that serializes calls
/// could only ever delay a send by that much. The send loop watches for that.
fn spawn_receiver(s: Arc<UdpSocket>, pc_ip: Option<IpAddr>) -> bool {
    if let Err(e) = s.set_read_timeout(Some(RECV_TIMEOUT)) {
        log::log(format!("inputs off: couldn't set a read timeout on the socket: {e}"));
        return false;
    }
    let gen = SOCKET_GEN.load(Ordering::Relaxed);
    let spawned = std::thread::Builder::new().name("super-cpu-rx".into()).spawn(move || {
        let mut rx = InputReceiver::new();
        let mut buf = [0u8; 64];
        let mut first = true;
        // The Switch reports some receive problems as a bare "os error 1".
        // Nothing here is fatal: log it (first time, then at 2, 4, 8, ...),
        // back off a little, and keep trying.
        let mut errors = 0u32;
        let mut in_a_row = 0u32;
        let mut noted_timeout_error = false;
        let mut dropped_timeout = false;
        while SOCKET_GEN.load(Ordering::Relaxed) == gen {
            let t = Instant::now();
            match s.recv_from(&mut buf) {
                Ok((n, from)) => {
                    in_a_row = 0;
                    if first {
                        first = false;
                        PC_SEEN.store(true, Ordering::Relaxed);
                        log::log(format!("first packet from the PC: {from}"));
                    }
                    if pc_ip.map_or(true, |ip| ip == from.ip()) {
                        rx.handle(&input::SHARED, &buf[..n], input::now_ms(), LIVE_FRAME.load(Ordering::Relaxed));
                    } else {
                        input::SHARED.rejected.fetch_add(1, Ordering::Relaxed);
                    }
                }
                Err(e) => {
                    let took = t.elapsed();
                    let usual = matches!(
                        e.kind(),
                        ErrorKind::WouldBlock | ErrorKind::TimedOut | ErrorKind::ConnectionReset | ErrorKind::ConnectionRefused
                    );
                    // A receive that waited out the timeout is a timeout,
                    // whatever error code it came back with.
                    if usual || took >= RECV_TIMEOUT * 3 / 4 {
                        if !usual && !noted_timeout_error {
                            noted_timeout_error = true;
                            log::log(format!("receive timeouts come back as \"{e}\" here, treating them as timeouts"));
                        }
                        if took < Duration::from_millis(1) {
                            sleep(Duration::from_millis(1));
                        }
                        continue;
                    }
                    errors += 1;
                    in_a_row += 1;
                    if errors.is_power_of_two() {
                        log::log(format!(
                            "receive error #{errors}: {e} ({:?}) after {took:?}, local addr {:?}, retrying",
                            e.kind(),
                            s.local_addr().map(|a| a.to_string()).unwrap_or_else(|e| format!("? ({e})"))
                        ));
                    }
                    if in_a_row == 200 && !dropped_timeout {
                        dropped_timeout = true;
                        let r = s.set_read_timeout(None);
                        log::log(format!("200 receive errors in a row, trying without a read timeout: {r:?}"));
                    }
                    sleep(Duration::from_millis(5));
                }
            }
        }
    });
    match spawned {
        Ok(_) => {
            log::log("inputs on: receive thread started");
            true
        }
        Err(e) => {
            log::log(format!("inputs off: couldn't start the receive thread: {e}"));
            false
        }
    }
}

const RECV_TIMEOUT: Duration = Duration::from_millis(20);
/// Sends slower than this are counted and reported: lots of them would mean
/// the socket layer makes sends wait for the blocked receive.
const SLOW_SEND: Duration = Duration::from_millis(4);
const SEND_REPORT_EVERY: Duration = Duration::from_secs(10);
const LOG_RESEND_EVERY: Duration = Duration::from_secs(5);

/// Runs forever on its own thread: forwards each finished frame as a state
/// packet, sends a heartbeat (plus a control status packet) every 500 ms,
/// sends an ack for each input the game applied, and forwards plugin log
/// lines (kind 6). Inputs come in on the receive thread. Rebinds if the
/// network drops. `health` is read fresh for each heartbeat.
pub fn run(cfg: Config, frames: Receiver<FrameState>, acks: Receiver<Ack>, health: fn() -> Health) {
    let target = format!("{}:{}", cfg.pc_ip, cfg.udp_port);
    let pc_ip: Option<IpAddr> = cfg.pc_ip.parse().ok();
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
        let sock = Arc::new(sock);
        SOCKET_GEN.fetch_add(1, Ordering::Relaxed);
        // Started after the first heartbeat is sent: the socket may not
        // have a local port until it has sent something.
        let mut receiving = false;
        let mut rx_started = false;
        log::log(format!("streaming to {target}"));
        let mut next_heartbeat = Instant::now();
        let mut next_log_resend = Instant::now();
        let mut logs_sent = 0u32;
        // Send timing, reported every SEND_REPORT_EVERY when any send was slow:
        // tells whether sends ever wait on the blocked receive.
        let (mut sends, mut slow_sends, mut slowest) = (0u32, 0u32, Duration::ZERO);
        let mut next_send_report = Instant::now() + SEND_REPORT_EVERY;
        'conn: loop {
            while let Ok(a) = acks.try_recv() {
                if !send(&sock, &ack_packet(seq, &a), &target) {
                    break 'conn;
                }
                seq = seq.wrapping_add(1);
            }
            let wait = next_heartbeat.saturating_duration_since(Instant::now()).min(ACK_POLL);
            match frames.recv_timeout(wait) {
                Ok(frame) => {
                    LIVE_FRAME.store(frame.frame, Ordering::Relaxed);
                    let t = Instant::now();
                    if !send(&sock, &state_packet(seq, &frame), &target) {
                        break;
                    }
                    let took = t.elapsed();
                    sends += 1;
                    slowest = slowest.max(took);
                    if took > SLOW_SEND {
                        slow_sends += 1;
                    }
                    seq = seq.wrapping_add(1);
                }
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => return,
            }
            // New log lines go out right away; the whole recent set again
            // every few seconds, so a listener started late still sees boot.
            // (Not while a bridge is talking to us: no bursts during play.)
            let resend = Instant::now() >= next_log_resend && !PC_SEEN.load(Ordering::Relaxed);
            if resend {
                next_log_resend = Instant::now() + LOG_RESEND_EVERY;
            }
            for (id, text) in log::pending(if resend { 0 } else { logs_sent }) {
                if !send(&sock, &log::log_packet(seq, id, &text), &target) {
                    break 'conn;
                }
                seq = seq.wrapping_add(1);
                logs_sent = logs_sent.max(id + 1);
            }
            if Instant::now() >= next_send_report {
                next_send_report = Instant::now() + SEND_REPORT_EVERY;
                if slow_sends > 0 {
                    log::log(format!(
                        "send timing ({}s): {slow_sends} of {sends} state sends over {SLOW_SEND:?}, slowest {slowest:?}, receiving {}",
                        SEND_REPORT_EVERY.as_secs(),
                        if receiving { "on" } else { "off" }
                    ));
                }
                (sends, slow_sends, slowest) = (0, 0, Duration::ZERO);
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
                if !rx_started {
                    rx_started = true;
                    receiving = spawn_receiver(Arc::clone(&sock), pc_ip);
                    input::RECEIVING.store(receiving, Ordering::Relaxed);
                }
            }
        }
        input::RECEIVING.store(false, Ordering::Relaxed);
        sleep(RETRY_AFTER);
    }
}

/// Upper bound on how long an ack waits before it's sent.
const ACK_POLL: Duration = Duration::from_millis(1);

#[cfg(test)]
mod tests {
    use super::*;

    /// The receive thread reads packets on the shared socket while this
    /// thread keeps sending on it, and sends stay fast.
    #[test]
    fn shared_socket_receives_while_sending() {
        let sock = Arc::new(UdpSocket::bind("127.0.0.1:0").unwrap());
        let addr = sock.local_addr().unwrap();
        let peer = UdpSocket::bind("127.0.0.1:0").unwrap();
        SOCKET_GEN.fetch_add(1, Ordering::Relaxed);
        assert!(spawn_receiver(Arc::clone(&sock), None));
        let before = input::SHARED.rejected.load(Ordering::Relaxed);
        let mut slowest = Duration::ZERO;
        for _ in 0..20 {
            peer.send_to(b"not an input packet", addr).unwrap();
            let t = Instant::now();
            sock.send_to(b"state", peer.local_addr().unwrap()).unwrap();
            slowest = slowest.max(t.elapsed());
            sleep(Duration::from_millis(2));
        }
        sleep(Duration::from_millis(50));
        SOCKET_GEN.fetch_add(1, Ordering::Relaxed); // let the thread quit
        assert!(input::SHARED.rejected.load(Ordering::Relaxed) - before >= 20);
        assert!(slowest < SLOW_SEND, "send took {slowest:?}");
    }

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
