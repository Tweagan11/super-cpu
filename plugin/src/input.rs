//! PC-to-Switch input path (SSB-6, SSB-7). Pure logic, no skyline or smash
//! types, so it compiles and tests on a normal PC too.
//!
//! Two threads touch this:
//! - the network thread (`net.rs`) parses input packets and drops them into
//!   a small ring keyed by `apply_at` (`InputReceiver`)
//! - the game thread (the final input mapping hook in `control.rs`) asks for
//!   the input meant for the frame about to run (`Applier`)
//!
//! The ring is lock-free (a seqlock per slot): the game thread never waits on
//! the network thread, it just reads, and if a slot happens to be mid-write it
//! treats that frame as "no fresh input".
//!
//! Failure rules, straight from the Phase 0 design:
//! - input whose `apply_at` already passed is dropped (counted as late)
//! - no fresh input for 1 to 6 frames: repeat the last one
//! - more than 6 frames: neutral
//! - no input packets for 2 s: the PC session is gone, stop touching the
//!   controller, the port goes back to normal control
//!
//! Packet layouts are in docs/protocol.md and bridge/bridge/wire.py.

use std::sync::atomic::{fence, AtomicI32, AtomicU32, AtomicU64, AtomicU8, Ordering};
use std::sync::mpsc::SyncSender;
use std::sync::{Mutex, OnceLock};
use std::time::Instant;

use crate::net::{write_header, HEADER_LEN, KIND_INPUT, MAGIC, PROTO_VERSION};

pub const KIND_ACK: u16 = 4;
pub const KIND_CONTROL_STATUS: u16 = 5;

/// header + for_frame + apply_at + 4 x i8 sticks + buttons
pub const INPUT_LEN: usize = HEADER_LEN + 16;
/// header + for_frame + applied_frame + ready_frame
pub const ACK_LEN: usize = HEADER_LEN + 12;
/// header + 4 bytes of port/flags + 10 counters + the game's own mapped inputs
pub const STATUS_LEN: usize = HEADER_LEN + 4 + 40 + 8;

/// No fresh input for up to this many frames repeats the last one.
pub const REPEAT_FRAMES: u32 = 6;
/// No input packets for this long means the PC session is gone.
pub const SESSION_TIMEOUT_MS: u64 = 2000;
/// Pending inputs, indexed by apply_at % RING. Inputs more than RING - 1
/// frames ahead of the game are rejected. 32 frames is over half a second.
pub const RING: usize = 32;
/// This many old-looking seq numbers in a row means the bridge restarted
/// (seq went back to 0), so start over instead of ignoring it forever.
const RESYNC_AFTER: u32 = 30;
/// The game thread counts as "running the hook" if it ran this recently.
const TARGET_FRESH_MS: u64 = 100;

/// One frame of controller input, same meaning as the game's MappedInputs.
#[derive(Clone, Copy, Default, PartialEq, Eq, Debug)]
pub struct Inputs {
    pub stick_x: i8,
    pub stick_y: i8,
    pub cstick_x: i8,
    pub cstick_y: i8,
    /// Mapped button bits, see bridge/bridge/buttons.py
    pub buttons: u32,
}

impl Inputs {
    pub const NEUTRAL: Inputs = Inputs { stick_x: 0, stick_y: 0, cstick_x: 0, cstick_y: 0, buttons: 0 };

    pub fn to_bits(self) -> u64 {
        (self.buttons as u64)
            | (self.stick_x as u8 as u64) << 32
            | (self.stick_y as u8 as u64) << 40
            | (self.cstick_x as u8 as u64) << 48
            | (self.cstick_y as u8 as u64) << 56
    }

    pub fn from_bits(b: u64) -> Inputs {
        Inputs {
            buttons: b as u32,
            stick_x: (b >> 32) as u8 as i8,
            stick_y: (b >> 40) as u8 as i8,
            cstick_x: (b >> 48) as u8 as i8,
            cstick_y: (b >> 56) as u8 as i8,
        }
    }
}

// ---------------------------------------------------------------------------
// Packets

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct InputPacket {
    pub seq: u32,
    pub for_frame: u32,
    pub apply_at: u32,
    pub inputs: Inputs,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Reject {
    Short,
    Magic,
    Version,
    Kind,
}

fn u16_at(b: &[u8], i: usize) -> u16 {
    u16::from_le_bytes([b[i], b[i + 1]])
}

fn u32_at(b: &[u8], i: usize) -> u32 {
    u32::from_le_bytes([b[i], b[i + 1], b[i + 2], b[i + 3]])
}

pub fn parse_input(buf: &[u8]) -> Result<InputPacket, Reject> {
    if buf.len() < HEADER_LEN {
        return Err(Reject::Short);
    }
    if buf[0..4] != MAGIC {
        return Err(Reject::Magic);
    }
    if u16_at(buf, 4) != PROTO_VERSION {
        return Err(Reject::Version);
    }
    if u16_at(buf, 6) != KIND_INPUT {
        return Err(Reject::Kind);
    }
    if buf.len() < INPUT_LEN {
        return Err(Reject::Short);
    }
    Ok(InputPacket {
        seq: u32_at(buf, 8),
        for_frame: u32_at(buf, 12),
        apply_at: u32_at(buf, 16),
        inputs: Inputs {
            stick_x: buf[20] as i8,
            stick_y: buf[21] as i8,
            cstick_x: buf[22] as i8,
            cstick_y: buf[23] as i8,
            buttons: u32_at(buf, 24),
        },
    })
}

/// Only used by tests and the host harness; the plugin never sends inputs.
#[allow(dead_code)]
pub fn input_packet(p: &InputPacket) -> [u8; INPUT_LEN] {
    let mut buf = [0u8; INPUT_LEN];
    write_header(&mut buf, KIND_INPUT, p.seq);
    buf[12..16].copy_from_slice(&p.for_frame.to_le_bytes());
    buf[16..20].copy_from_slice(&p.apply_at.to_le_bytes());
    buf[20] = p.inputs.stick_x as u8;
    buf[21] = p.inputs.stick_y as u8;
    buf[22] = p.inputs.cstick_x as u8;
    buf[23] = p.inputs.cstick_y as u8;
    buf[24..28].copy_from_slice(&p.inputs.buttons.to_le_bytes());
    buf
}

/// Sent once per fresh input applied (repeats and neutral frames don't ack).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Ack {
    /// the state frame the PC answered
    pub for_frame: u32,
    /// the frame it took effect on (always its apply_at, late ones are dropped)
    pub applied_frame: u32,
    /// the first frame it could have taken effect on, given when it arrived.
    /// ready_frame - for_frame is the real round trip.
    pub ready_frame: u32,
}

pub fn ack_packet(seq: u32, a: &Ack) -> [u8; ACK_LEN] {
    let mut buf = [0u8; ACK_LEN];
    write_header(&mut buf, KIND_ACK, seq);
    buf[12..16].copy_from_slice(&a.for_frame.to_le_bytes());
    buf[16..20].copy_from_slice(&a.applied_frame.to_le_bytes());
    buf[20..24].copy_from_slice(&a.ready_frame.to_le_bytes());
    buf
}

pub const FLAG_HOOK_INSTALLED: u8 = 1;
pub const FLAG_SESSION_ACTIVE: u8 = 2;
pub const FLAG_RECEIVING: u8 = 4;
pub const FLAG_CPU_PROBE: u8 = 8;
/// `fim_player_idx` is set in config.toml (the port to player_idx mapping
/// isn't the default one).
pub const FLAG_FIM_OVERRIDE: u8 = 16;
/// drive_cpu is on: the bot is a CPU slot and FIM leaves its port alone.
pub const FLAG_DRIVE_CPU: u8 = 32;

/// Everything in a control status packet (kind 5, sent with each heartbeat).
#[derive(Clone, Copy, Default, PartialEq, Eq, Debug)]
pub struct Status {
    pub bot_port: u8,
    pub bot_player_idx: i8,
    pub flags: u8,
    /// player_idx captured as the port that isn't the bot (v3, was reserved)
    pub other_player_idx: i8,
    pub fim_calls: u32,
    pub idx_seen: u32,
    pub inputs_rx: u32,
    pub applied: u32,
    pub late: u32,
    pub rejected: u32,
    pub repeats: u32,
    pub neutral: u32,
    /// set_cpu_controls probe: calls, and a bitmask of controller_index values seen
    pub cpu_calls: u32,
    pub cpu_idx_seen: u32,
    /// What the game itself mapped for the bot's controller on the last call,
    /// before we overwrote it. Lets you check stick range and button bits on
    /// hardware with the bridge stopped.
    pub orig: Inputs,
}

pub fn status_packet(seq: u32, s: &Status) -> [u8; STATUS_LEN] {
    let mut buf = [0u8; STATUS_LEN];
    write_header(&mut buf, KIND_CONTROL_STATUS, seq);
    buf[12] = s.bot_port;
    buf[13] = s.bot_player_idx as u8;
    buf[14] = s.flags;
    buf[15] = s.other_player_idx as u8;
    let counters = [
        s.fim_calls, s.idx_seen, s.inputs_rx, s.applied, s.late, s.rejected, s.repeats, s.neutral,
        s.cpu_calls, s.cpu_idx_seen,
    ];
    for (i, c) in counters.iter().enumerate() {
        buf[16 + 4 * i..20 + 4 * i].copy_from_slice(&c.to_le_bytes());
    }
    buf[56] = s.orig.stick_x as u8;
    buf[57] = s.orig.stick_y as u8;
    buf[58] = s.orig.cstick_x as u8;
    buf[59] = s.orig.cstick_y as u8;
    buf[60..64].copy_from_slice(&s.orig.buttons.to_le_bytes());
    buf
}

// ---------------------------------------------------------------------------
// Shared state between the network thread and the game thread

/// One pending input (or one captured input, see `Capture`). Seqlock: each
/// slot has exactly one writer thread, readers retry a few times if they
/// land mid-write and give up rather than wait.
struct Slot {
    ver: AtomicU32,
    /// apply_at << 32 | for_frame, EMPTY when unused
    key: AtomicU64,
    inputs: AtomicU64,
    ready: AtomicU32,
}

const EMPTY: u64 = u64::MAX;

impl Slot {
    const fn new() -> Slot {
        Slot {
            ver: AtomicU32::new(0),
            key: AtomicU64::new(EMPTY),
            inputs: AtomicU64::new(0),
            ready: AtomicU32::new(0),
        }
    }

    /// One writer thread per slot.
    fn write(&self, key: u64, inputs: u64, ready: u32) {
        let v = self.ver.load(Ordering::Relaxed);
        self.ver.store(v.wrapping_add(1), Ordering::Relaxed);
        fence(Ordering::Release);
        self.key.store(key, Ordering::Relaxed);
        self.inputs.store(inputs, Ordering::Relaxed);
        self.ready.store(ready, Ordering::Relaxed);
        self.ver.store(v.wrapping_add(2), Ordering::Release);
    }

    /// Never blocks. None if the writer kept getting in the way.
    fn read(&self) -> Option<(u64, u64, u32)> {
        for _ in 0..4 {
            let v1 = self.ver.load(Ordering::Acquire);
            if v1 & 1 == 1 {
                std::hint::spin_loop();
                continue;
            }
            let key = self.key.load(Ordering::Relaxed);
            let inputs = self.inputs.load(Ordering::Relaxed);
            let ready = self.ready.load(Ordering::Relaxed);
            fence(Ordering::Acquire);
            if self.ver.load(Ordering::Relaxed) == v1 {
                return Some((key, inputs, ready));
            }
        }
        None
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Pending {
    pub for_frame: u32,
    pub apply_at: u32,
    pub inputs: Inputs,
    pub ready: u32,
}

pub struct Shared {
    slots: [Slot; RING],
    /// now_ms() of the last accepted input packet, 0 = never
    last_rx_ms: AtomicU64,
    /// last frame the game thread ran the bot's input mapping for, and when
    last_target: AtomicU32,
    last_target_ms: AtomicU64,
    /// the game's own mapped inputs for the bot's controller (Inputs::to_bits)
    orig: AtomicU64,
    pub fim_calls: AtomicU32,
    pub idx_seen: AtomicU32,
    pub inputs_rx: AtomicU32,
    pub applied: AtomicU32,
    pub late: AtomicU32,
    pub rejected: AtomicU32,
    pub repeats: AtomicU32,
    pub neutral: AtomicU32,
    pub cpu_calls: AtomicU32,
    pub cpu_idx_seen: AtomicU32,
}

impl Shared {
    pub const fn new() -> Shared {
        const S: Slot = Slot::new();
        Shared {
            slots: [S; RING],
            last_rx_ms: AtomicU64::new(0),
            last_target: AtomicU32::new(0),
            last_target_ms: AtomicU64::new(0),
            orig: AtomicU64::new(0),
            fim_calls: AtomicU32::new(0),
            idx_seen: AtomicU32::new(0),
            inputs_rx: AtomicU32::new(0),
            applied: AtomicU32::new(0),
            late: AtomicU32::new(0),
            rejected: AtomicU32::new(0),
            repeats: AtomicU32::new(0),
            neutral: AtomicU32::new(0),
            cpu_calls: AtomicU32::new(0),
            cpu_idx_seen: AtomicU32::new(0),
        }
    }

    pub fn session_active(&self, now_ms: u64) -> bool {
        let t = self.last_rx_ms.load(Ordering::Relaxed);
        t != 0 && now_ms.saturating_sub(t) < SESSION_TIMEOUT_MS
    }

    /// Network thread only.
    fn clear(&self) {
        for s in &self.slots {
            s.write(EMPTY, 0, 0);
        }
    }

    /// Network thread only.
    fn put(&self, p: &Pending) {
        let key = (p.apply_at as u64) << 32 | p.for_frame as u64;
        self.slots[p.apply_at as usize % RING].write(key, p.inputs.to_bits(), p.ready);
    }

    /// Game thread: the input meant for exactly this frame, if one arrived.
    fn get(&self, frame: u32) -> Option<Pending> {
        let (key, inputs, ready) = self.slots[frame as usize % RING].read()?;
        if key == EMPTY || (key >> 32) as u32 != frame {
            return None;
        }
        Some(Pending { for_frame: key as u32, apply_at: frame, inputs: Inputs::from_bits(inputs), ready })
    }

    /// The first frame a packet arriving now could still make. Uses what the
    /// game thread last told us, or the state frame counter if the bot's
    /// input mapping isn't running (wrong port, hook missing).
    fn next_target(&self, now_ms: u64, live_frame: u32) -> u32 {
        let t = self.last_target_ms.load(Ordering::Relaxed);
        if t != 0 && now_ms.saturating_sub(t) < TARGET_FRESH_MS {
            self.last_target.load(Ordering::Relaxed).wrapping_add(1)
        } else {
            live_frame.wrapping_add(1)
        }
    }

    pub fn status(&self, bot_port: u8, bot_player_idx: i8, flags: u8) -> Status {
        let r = |a: &AtomicU32| a.load(Ordering::Relaxed);
        Status {
            bot_port,
            bot_player_idx,
            flags,
            other_player_idx: 0,
            fim_calls: r(&self.fim_calls),
            idx_seen: r(&self.idx_seen),
            inputs_rx: r(&self.inputs_rx),
            applied: r(&self.applied),
            late: r(&self.late),
            rejected: r(&self.rejected),
            repeats: r(&self.repeats),
            neutral: r(&self.neutral),
            cpu_calls: r(&self.cpu_calls),
            cpu_idx_seen: r(&self.cpu_idx_seen),
            orig: Inputs::from_bits(self.orig.load(Ordering::Relaxed)),
        }
    }
}

fn bump(a: &AtomicU32) {
    a.fetch_add(1, Ordering::Relaxed);
}

// ---------------------------------------------------------------------------
// Input capture (SSB-13): what every port's controller said each frame

/// Frames of captured input kept per port. The state packet for frame N goes
/// out one frame after N's inputs are mapped, so 2 would do; 4 leaves slack.
pub const CAPTURE_SLOTS: usize = 4;

/// Per-frame input flags carried in the state packet (v3).
pub const CAP_P1_VALID: u32 = 1;
pub const CAP_P2_VALID: u32 = 2;
/// The PC wrote this port's inputs (the bot, during a session).
pub const CAP_P1_DRIVEN: u32 = 4;
pub const CAP_P2_DRIVEN: u32 = 8;
/// v4: the ControlModule block for this port is valid (read from the
/// fighter's own ControlModule, works for CPU ports too).
pub const CAP_P1_CM: u32 = 16;
pub const CAP_P2_CM: u32 = 32;

/// Float stick (ControlModule, -1..1) to the i8 we log: x127, rounded,
/// clamped. NaN reads as centered.
pub fn stick_to_i8(v: f32) -> i8 {
    if v.is_nan() {
        return 0;
    }
    (v * 127.0).round().clamp(-128.0, 127.0) as i8
}

/// ControlModule button translation. `check_button_on(boma, CONTROL_PAD_BUTTON_*)`
/// takes the pad button index below (values from skyline-smash lua_const.rs,
/// re-checked against the real constants on the first fighter frame, see
/// `state::verify_pad_indices`); the second column is the MappedInputs bit
/// from bridge/bridge/buttons.py. The two layouts only agree for attack,
/// special, jump and guard (indices 0..3), so everything goes through here.
/// Every pad button has a mapped bit; nothing is dropped.
pub const PAD_BUTTONS: [(i32, u32, &str); 18] = [
    (0x0, 0x1, "ATTACK"),
    (0x1, 0x2, "SPECIAL"),
    (0x2, 0x4, "JUMP"),
    (0x3, 0x8, "GUARD"),
    (0x4, 0x20, "SMASH"),
    (0x5, 0x400, "APPEAL_HI"),
    (0x6, 0x1000, "APPEAL_LW"),
    (0x7, 0x2000, "APPEAL_S_L"),
    (0x8, 0x4000, "APPEAL_S_R"),
    (0x9, 0x10, "CATCH"),
    (0xA, 0x40, "JUMP_MINI"),
    (0xB, 0x80, "CSTICK_ON"),
    (0xC, 0x8000, "FLICK_JUMP"),
    (0xD, 0x100, "STOCK_SHARE"),
    (0xE, 0x200, "ATTACK_RAW"),
    (0xF, 0x800, "SPECIAL_RAW"),
    (0x10, 0x10000, "GUARD_HOLD"),
    (0x11, 0x20000, "SPECIAL_RAW2"),
];

/// MappedInputs-layout buttons from a "is this pad button held" query.
pub fn cm_buttons(mut on: impl FnMut(i32) -> bool) -> u32 {
    PAD_BUTTONS.iter().fold(0, |acc, &(idx, bit, _)| if on(idx) { acc | bit } else { acc })
}

/// The final mapped inputs for ports 1 and 2, keyed by the frame that reads
/// them. Written by the final input mapping hook (one writer per port), read
/// by the fighter hook when it publishes a frame. Lock-free both ways.
pub struct Capture {
    slots: [[Slot; CAPTURE_SLOTS]; 2],
}

impl Capture {
    pub const fn new() -> Capture {
        const S: Slot = Slot::new();
        const ROW: [Slot; CAPTURE_SLOTS] = [S; CAPTURE_SLOTS];
        Capture { slots: [ROW; 2] }
    }

    /// `port` is 0 for port 1, 1 for port 2. `frame` is the frame these
    /// inputs take effect on (live_frame + 1). Called again for the same
    /// frame just overwrites.
    pub fn put(&self, port: usize, frame: u32, inputs: Inputs, driven: bool) {
        if port < 2 {
            self.slots[port][frame as usize % CAPTURE_SLOTS].write(frame as u64, inputs.to_bits(), driven as u32);
        }
    }

    /// What `port` had on `frame`, and whether the PC drove it. None if that
    /// frame was never captured (the slot holds another frame) or the
    /// writer was mid-write.
    pub fn get(&self, port: usize, frame: u32) -> Option<(Inputs, bool)> {
        let (key, inputs, driven) = self.slots.get(port)?[frame as usize % CAPTURE_SLOTS].read()?;
        (key == frame as u64).then(|| (Inputs::from_bits(inputs), driven != 0))
    }

    /// Both ports for one frame, in the shape the state packet wants.
    pub fn frame(&self, frame: u32) -> ([Inputs; 2], u32) {
        let mut out = [Inputs::NEUTRAL; 2];
        let mut flags = 0;
        for port in 0..2 {
            if let Some((i, driven)) = self.get(port, frame) {
                out[port] = i;
                flags |= if port == 0 { CAP_P1_VALID } else { CAP_P2_VALID };
                if driven {
                    flags |= if port == 0 { CAP_P1_DRIVEN } else { CAP_P2_DRIVEN };
                }
            }
        }
        (out, flags)
    }
}

/// Which final input mapping player_idx gets captured as port 1 and port 2.
///
/// The bot's port uses `bot_player_idx` (config `fim_player_idx`, default
/// bot_port - 1). The other port uses its own port - 1, unless that's the
/// index the bot took, in which case the two swap. So the override that
/// docs/hooks.md suggests for "player_idx is the controller slot" flips both
/// ports, not just the bot's. Still unverified on hardware.
pub fn capture_map(bot_port: u8, bot_player_idx: i32) -> [i32; 2] {
    let mut m = [0, 1];
    for (p, idx) in m.iter_mut().enumerate() {
        if p + 1 == bot_port as usize {
            *idx = bot_player_idx;
        } else if p as i32 == bot_player_idx {
            *idx = bot_port as i32 - 1;
        }
    }
    m
}

// ---------------------------------------------------------------------------
// Network thread side

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Outcome {
    Queued,
    Bad(Reject),
    OldSeq,
    Late,
    TooFar,
}

/// Runs on the network thread. Keeps only the newest seq, drops late input,
/// and files the rest under its apply_at for the game thread.
#[derive(Default)]
pub struct InputReceiver {
    last_seq: Option<u32>,
    old_streak: u32,
    last_for: Option<u32>,
}

impl InputReceiver {
    pub fn new() -> Self {
        Self::default()
    }

    /// `live_frame` is the state frame counter (`state::current_frame()`).
    pub fn handle(&mut self, shared: &Shared, buf: &[u8], now_ms: u64, live_frame: u32) -> Outcome {
        let p = match parse_input(buf) {
            Ok(p) => p,
            Err(e) => {
                bump(&shared.rejected);
                return Outcome::Bad(e);
            }
        };
        if !shared.session_active(now_ms) {
            // New session: forget the last one's seq numbers and inputs.
            self.last_seq = None;
            self.last_for = None;
            self.old_streak = 0;
            shared.clear();
        }
        if let Some(last) = self.last_seq {
            let step = p.seq.wrapping_sub(last);
            if step == 0 || step > 0x7FFF_FFFF {
                self.old_streak += 1;
                if self.old_streak < RESYNC_AFTER {
                    bump(&shared.rejected);
                    return Outcome::OldSeq;
                }
                // The bridge restarted inside the 2 s window. Start over.
                shared.clear();
                self.last_for = None;
            }
        }
        self.old_streak = 0;
        self.last_seq = Some(p.seq);
        shared.last_rx_ms.store(now_ms.max(1), Ordering::Relaxed);
        bump(&shared.inputs_rx);

        // for_frame jumping way back means a new match (frames restart at 0).
        // Throw away whatever the last match left in the ring.
        if let Some(lf) = self.last_for {
            if lf as u64 > p.for_frame as u64 + RING as u64 {
                shared.clear();
            }
        }
        self.last_for = Some(p.for_frame);

        let next = shared.next_target(now_ms, live_frame);
        if p.apply_at < next {
            bump(&shared.late);
            return Outcome::Late;
        }
        if p.apply_at - next >= RING as u32 {
            bump(&shared.rejected);
            return Outcome::TooFar;
        }
        shared.put(&Pending { for_frame: p.for_frame, apply_at: p.apply_at, inputs: p.inputs, ready: next });
        Outcome::Queued
    }
}

// ---------------------------------------------------------------------------
// Game thread side

/// Runs on the game thread, once per frame for the bot's controller. Decides
/// what the bot's controller says this frame: fresh input, a repeat, neutral,
/// or "leave it alone" (no PC session).
#[derive(Default)]
pub struct Applier {
    last_target: Option<u32>,
    last_out: Inputs,
    last_fresh: Option<Inputs>,
    since_fresh: u32,
}

impl Applier {
    pub const fn new() -> Self {
        Applier { last_target: None, last_out: Inputs::NEUTRAL, last_fresh: None, since_fresh: 0 }
    }

    /// `target` is the frame the input will take effect on. Returns None to
    /// leave the game's own input alone. Calls `ack` once per fresh input.
    pub fn step(&mut self, shared: &Shared, target: u32, now_ms: u64, ack: &mut dyn FnMut(Ack)) -> Option<Inputs> {
        shared.last_target.store(target, Ordering::Relaxed);
        shared.last_target_ms.store(now_ms.max(1), Ordering::Relaxed);

        if !shared.session_active(now_ms) {
            *self = Applier::new();
            return None;
        }
        // Called again for the same frame (pause, or the game mapping twice):
        // say the same thing, don't count it as another frame.
        if self.last_target == Some(target) {
            return Some(self.last_out);
        }
        if let Some(prev) = self.last_target {
            if target < prev {
                // New match. Don't carry the old match's last input over.
                self.last_fresh = None;
                self.since_fresh = 0;
            }
        }
        self.last_target = Some(target);

        let out = match shared.get(target) {
            Some(p) if p.for_frame <= target => {
                bump(&shared.applied);
                ack(Ack { for_frame: p.for_frame, applied_frame: target, ready_frame: p.ready });
                self.last_fresh = Some(p.inputs);
                self.since_fresh = 0;
                p.inputs
            }
            _ => {
                self.since_fresh = self.since_fresh.saturating_add(1);
                match self.last_fresh {
                    Some(i) if self.since_fresh <= REPEAT_FRAMES => {
                        bump(&shared.repeats);
                        i
                    }
                    _ => {
                        bump(&shared.neutral);
                        Inputs::NEUTRAL
                    }
                }
            }
        };
        self.last_out = out;
        Some(out)
    }
}

// ---------------------------------------------------------------------------
// Plugin-wide instance and the glue the hook and network thread call

pub static SHARED: Shared = Shared::new();
static APPLIER: Mutex<Applier> = Mutex::new(Applier::new());
static ACK_TX: OnceLock<SyncSender<Ack>> = OnceLock::new();
static CLOCK: OnceLock<Instant> = OnceLock::new();

static BOT_PORT: AtomicU8 = AtomicU8::new(2);
static BOT_PLAYER_IDX: AtomicI32 = AtomicI32::new(1);
static FIM_OVERRIDE: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
/// drive_cpu: the CPU hook applies the PC's inputs, the FIM hook doesn't.
static DRIVE_CPU: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
/// player_idx captured as port 1 and port 2 (`capture_map`).
static CAPTURE_IDX: [AtomicI32; 2] = [AtomicI32::new(0), AtomicI32::new(1)];
/// Every port's final mapped inputs, read by state.rs when it publishes a frame.
pub static CAPTURE: Capture = Capture::new();
/// Set by control.rs once the final input mapping hook is in.
pub static HOOK_INSTALLED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
/// Set by net.rs when the socket can receive (read timeout accepted).
pub static RECEIVING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
/// Set by control.rs once the read-only set_cpu_controls probe is in.
pub static CPU_PROBE_INSTALLED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Milliseconds on a clock both threads share. Never 0.
pub fn now_ms() -> u64 {
    CLOCK.get_or_init(Instant::now).elapsed().as_millis() as u64 + 1
}

/// `bot_port` is 1-based. `bot_player_idx` is the final input mapping's
/// player_idx to drive (normally bot_port - 1, see docs/hooks.md).
/// `fim_override` is true when config.toml set `fim_player_idx`.
pub fn init(bot_port: u8, bot_player_idx: i32, fim_override: bool, acks: SyncSender<Ack>) {
    let _ = CLOCK.get_or_init(Instant::now);
    BOT_PORT.store(bot_port, Ordering::Relaxed);
    BOT_PLAYER_IDX.store(bot_player_idx, Ordering::Relaxed);
    FIM_OVERRIDE.store(fim_override, Ordering::Relaxed);
    for (a, idx) in CAPTURE_IDX.iter().zip(capture_map(bot_port, bot_player_idx)) {
        a.store(idx, Ordering::Relaxed);
    }
    let _ = ACK_TX.set(acks);
}

pub fn set_drive_cpu(on: bool) {
    DRIVE_CPU.store(on, Ordering::Relaxed);
}

pub fn drive_cpu() -> bool {
    DRIVE_CPU.load(Ordering::Relaxed)
}

pub fn bot_port() -> u8 {
    BOT_PORT.load(Ordering::Relaxed)
}

pub fn status() -> Status {
    let mut flags = 0;
    if HOOK_INSTALLED.load(Ordering::Relaxed) {
        flags |= FLAG_HOOK_INSTALLED;
    }
    if SHARED.session_active(now_ms()) {
        flags |= FLAG_SESSION_ACTIVE;
    }
    if RECEIVING.load(Ordering::Relaxed) {
        flags |= FLAG_RECEIVING;
    }
    if CPU_PROBE_INSTALLED.load(Ordering::Relaxed) {
        flags |= FLAG_CPU_PROBE;
    }
    if FIM_OVERRIDE.load(Ordering::Relaxed) {
        flags |= FLAG_FIM_OVERRIDE;
    }
    if drive_cpu() {
        flags |= FLAG_DRIVE_CPU;
    }
    let mut s = SHARED.status(bot_port(), BOT_PLAYER_IDX.load(Ordering::Relaxed) as i8, flags);
    // the port that isn't the bot (bot_port is 1 or 2 in practice; 3+ means
    // neither captured port is the bot and port 2 is reported)
    let other = if bot_port() == 2 { 0 } else { 1 };
    s.other_player_idx = CAPTURE_IDX[other].load(Ordering::Relaxed) as i8;
    s
}

/// set_cpu_controls probe, game thread. Read only: records which
/// controller_index values the CPU AI writes for (see docs/hooks.md).
pub fn on_cpu_controls(controller_index: i32) {
    bump(&SHARED.cpu_calls);
    SHARED.cpu_idx_seen.fetch_or(seen_bit(controller_index), Ordering::Relaxed);
}

/// Bits 0..30 for those values, bit 31 for anything else (negative, or big
/// like the handheld npad id 0x20).
pub fn seen_bit(v: i32) -> u32 {
    if (0..31).contains(&v) {
        1 << v
    } else {
        1 << 31
    }
}

/// Everything the final input mapping hook does, minus the game's types.
/// Game thread only. Returns the inputs to write, or None to leave the
/// game's own mapping alone.
///
/// `live_frame` is `state::live_frame()`: the last frame the fighters
/// finished, or None outside a match (menus, pause, results screen). The
/// input mapped now is what the fighters read on their next update, which
/// state.rs numbers live_frame + 1, so that's the frame we look up.
/// Outside a match we never touch the controller, even mid-session.
pub fn on_final_input_mapping(player_idx: i32, orig: Inputs, live_frame: Option<u32>) -> Option<Inputs> {
    bump(&SHARED.fim_calls);
    SHARED.idx_seen.fetch_or(seen_bit(player_idx), Ordering::Relaxed);
    let target = live_frame?.wrapping_add(1);
    let drive_cpu = drive_cpu();
    let out = if player_idx == BOT_PLAYER_IDX.load(Ordering::Relaxed) && !drive_cpu {
        apply_bot(target, orig)
    } else {
        None
    };
    // SSB-13: record what this port's fighter will actually read on
    // `target`, for the state packet. Two atomic compares and one seqlock
    // write, no locks.
    for (port, idx) in CAPTURE_IDX.iter().enumerate() {
        // drive_cpu: the bot's port is captured by the CPU hook (what was
        // really applied), not from a controller that may not even exist.
        if drive_cpu && port + 1 == bot_port() as usize {
            continue;
        }
        if idx.load(Ordering::Relaxed) == player_idx {
            CAPTURE.put(port, target, out.unwrap_or(orig), out.is_some());
        }
    }
    out
}

/// The applier step shared by both hooks: what the bot's controls say on
/// `target`, or None to leave the game's own input alone (no PC session).
/// `orig` is what the game had there (reported in the control status).
fn apply_bot(target: u32, orig: Inputs) -> Option<Inputs> {
    SHARED.orig.store(orig.to_bits(), Ordering::Relaxed);
    let mut applier = match APPLIER.lock() {
        Ok(a) => a,
        Err(poisoned) => poisoned.into_inner(),
    };
    applier.step(&SHARED, target, now_ms(), &mut |a| {
        if let Some(tx) = ACK_TX.get() {
            // never block the game; a full queue just loses the ack
            let _ = tx.try_send(a);
        }
    })
}

/// drive_cpu: the CPU hook's call for the bot's slot, game thread. `ai` is
/// what the CPU AI just decided, `live_frame` is `state::live_frame()`. Same
/// frame rule as the FIM hook (the next fighter update reads what we write).
/// Returns the inputs to write into the CPU's controls, or None to leave the
/// AI's decision. Records the result in the capture ring either way.
pub fn on_cpu_drive(ai: Inputs, live_frame: Option<u32>) -> Option<Inputs> {
    // Unlike the FIM hook, the CPU hook runs inside the fighter update for
    // the frame live_frame() already names, so what we write here is read
    // on that frame, not the next. Hardware (2026-10-16): with +1, the
    // control module on frame N read what was filed for N+1 every time, and
    // inputs counted as late a frame too soon (~20% late at delay 3).
    let target = live_frame?;
    let out = apply_bot(target, ai);
    let port = (bot_port() as usize).saturating_sub(1);
    CAPTURE.put(port, target, out.unwrap_or(ai), out.is_some());
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pkt(seq: u32, for_frame: u32, apply_at: u32, stick_x: i8) -> [u8; INPUT_LEN] {
        input_packet(&InputPacket {
            seq,
            for_frame,
            apply_at,
            inputs: Inputs { stick_x, ..Inputs::NEUTRAL },
        })
    }

    fn hex(b: &[u8]) -> String {
        b.iter().map(|x| format!("{x:02x}")).collect()
    }

    // The same hex strings are checked in bridge/tests/test_inputs.py, so a
    // layout change on either side breaks a test.
    const GOLDEN_INPUT: &str = "534350550400020007000000e8030000ea0300007f81050a05000000";
    const GOLDEN_ACK: &str = "534350550400040009000000e8030000ea030000e9030000";
    const GOLDEN_STATUS: &str = concat!(
        "53435055040005000b000000", "02010700",
        "e8030000", "03000000", "e2030000", "de030000",
        "01000000", "02000000", "03000000", "04000000",
        "40060000", "06000000",
        "78880000", "04000000",
    );

    #[test]
    fn sizes() {
        assert_eq!(INPUT_LEN, 28);
        assert_eq!(ACK_LEN, 24);
        assert_eq!(STATUS_LEN, 64);
    }

    #[test]
    fn golden_bytes() {
        let p = InputPacket {
            seq: 7,
            for_frame: 1000,
            apply_at: 1002,
            inputs: Inputs { stick_x: 127, stick_y: -127, cstick_x: 5, cstick_y: 10, buttons: 0x5 },
        };
        assert_eq!(hex(&input_packet(&p)), GOLDEN_INPUT);
        assert_eq!(parse_input(&input_packet(&p)), Ok(p));

        let a = Ack { for_frame: 1000, applied_frame: 1002, ready_frame: 1001 };
        assert_eq!(hex(&ack_packet(9, &a)), GOLDEN_ACK);

        let s = Status {
            bot_port: 2,
            bot_player_idx: 1,
            flags: 7,
            other_player_idx: 0,
            fim_calls: 1000,
            idx_seen: 3,
            inputs_rx: 994,
            applied: 990,
            late: 1,
            rejected: 2,
            repeats: 3,
            neutral: 4,
            cpu_calls: 1600,
            cpu_idx_seen: 6,
            orig: Inputs { stick_x: 120, stick_y: -120, cstick_x: 0, cstick_y: 0, buttons: 4 },
        };
        assert_eq!(hex(&status_packet(11, &s)), GOLDEN_STATUS);
    }

    #[test]
    fn rejects_bad_packets() {
        let good = pkt(0, 10, 12, 0);
        assert_eq!(parse_input(&good[..10]), Err(Reject::Short));
        assert_eq!(parse_input(&good[..20]), Err(Reject::Short));
        let mut b = good;
        b[0] = b'X';
        assert_eq!(parse_input(&b), Err(Reject::Magic));
        let mut b = good;
        b[4] = 9;
        assert_eq!(parse_input(&b), Err(Reject::Version));
        let mut b = good;
        b[6] = 1;
        assert_eq!(parse_input(&b), Err(Reject::Kind));
    }

    #[test]
    fn inputs_bits_round_trip() {
        let i = Inputs { stick_x: -128, stick_y: 127, cstick_x: -1, cstick_y: 1, buttons: 0x2_0001 };
        assert_eq!(Inputs::from_bits(i.to_bits()), i);
    }

    /// Drives receiver and applier like the plugin does: the game is about
    /// to run `frame`, the newest state the PC has seen is frame - 2.
    struct Sim {
        shared: Shared,
        rx: InputReceiver,
        ap: Applier,
        now: u64,
        acks: Vec<Ack>,
    }

    impl Sim {
        fn new() -> Self {
            Sim { shared: Shared::new(), rx: InputReceiver::new(), ap: Applier::new(), now: 1000, acks: vec![] }
        }
        fn recv(&mut self, seq: u32, for_frame: u32, apply_at: u32, x: i8, live: u32) -> Outcome {
            let p = pkt(seq, for_frame, apply_at, x);
            self.rx.handle(&self.shared, &p, self.now, live)
        }
        fn frame(&mut self, target: u32) -> Option<Inputs> {
            let acks = &mut self.acks;
            self.ap.step(&self.shared, target, self.now, &mut |a| acks.push(a))
        }
        fn x(&mut self, target: u32) -> Option<i8> {
            self.frame(target).map(|i| i.stick_x)
        }
    }

    #[test]
    fn no_session_means_hands_off() {
        let mut s = Sim::new();
        for f in 1..100 {
            assert_eq!(s.frame(f), None);
        }
        assert_eq!(s.shared.neutral.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn applies_on_apply_at_and_acks() {
        let mut s = Sim::new();
        s.frame(10); // game thread is running, last target 10
        assert_eq!(s.recv(0, 9, 11, 50, 9), Outcome::Queued);
        assert_eq!(s.recv(1, 10, 12, 60, 9), Outcome::Queued);
        assert_eq!(s.x(11), Some(50));
        assert_eq!(s.x(12), Some(60));
        assert_eq!(
            s.acks,
            vec![
                Ack { for_frame: 9, applied_frame: 11, ready_frame: 11 },
                Ack { for_frame: 10, applied_frame: 12, ready_frame: 11 },
            ]
        );
    }

    #[test]
    fn late_input_is_dropped() {
        let mut s = Sim::new();
        s.recv(0, 1, 3, 1, 0); // start the session
        s.frame(20);
        assert_eq!(s.recv(1, 18, 20, 99, 19), Outcome::Late);
        assert_eq!(s.recv(2, 19, 21, 42, 19), Outcome::Queued);
        assert_eq!(s.x(21), Some(42));
        assert_eq!(s.shared.late.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn repeats_then_neutral() {
        let mut s = Sim::new();
        s.frame(0);
        s.recv(0, 0, 1, 77, 0);
        assert_eq!(s.x(1), Some(77));
        for f in 2..=7 {
            assert_eq!(s.x(f), Some(77), "frame {f} should repeat");
        }
        assert_eq!(s.x(8), Some(0), "7 frames without input goes neutral");
        assert_eq!(s.x(9), Some(0));
        assert_eq!(s.shared.repeats.load(Ordering::Relaxed), 6);
        assert_eq!(s.shared.neutral.load(Ordering::Relaxed), 2);
        // fresh input recovers immediately
        s.recv(1, 8, 10, 5, 9);
        assert_eq!(s.x(10), Some(5));
    }

    #[test]
    fn neutral_if_nothing_ever_arrived_for_this_match() {
        let mut s = Sim::new();
        s.frame(0);
        s.recv(0, 0, 50, 3, 0); // session open, input far ahead
        assert_eq!(s.x(1), Some(0));
    }

    #[test]
    fn same_frame_twice_is_not_a_new_frame() {
        let mut s = Sim::new();
        s.frame(0);
        s.recv(0, 0, 1, 9, 0);
        assert_eq!(s.x(1), Some(9));
        for _ in 0..20 {
            assert_eq!(s.x(1), Some(9)); // paused: same answer, no repeats counted
        }
        assert_eq!(s.acks.len(), 1);
        assert_eq!(s.shared.repeats.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn session_times_out_after_2s() {
        let mut s = Sim::new();
        s.frame(0);
        s.recv(0, 0, 1, 9, 0);
        assert_eq!(s.x(1), Some(9));
        s.now += SESSION_TIMEOUT_MS - 1;
        assert_eq!(s.x(2), Some(9), "still in the session, so it repeats");
        s.now += 1;
        assert_eq!(s.frame(3), None, "2 s without a packet hands control back");
        // and a fresh packet opens a new session
        assert_eq!(s.recv(0, 3, 5, 4, 3), Outcome::Queued);
        assert_eq!(s.x(4), Some(0));
        assert_eq!(s.x(5), Some(4));
    }

    #[test]
    fn keeps_newest_seq_only() {
        let mut s = Sim::new();
        s.frame(0);
        assert_eq!(s.recv(5, 0, 2, 1, 0), Outcome::Queued);
        assert_eq!(s.recv(4, 0, 3, 2, 0), Outcome::OldSeq);
        assert_eq!(s.recv(5, 0, 3, 2, 0), Outcome::OldSeq);
        assert_eq!(s.recv(6, 1, 3, 3, 0), Outcome::Queued);
        // wraparound is "newer"
        let mut s = Sim::new();
        s.frame(0);
        assert_eq!(s.recv(u32::MAX, 0, 2, 1, 0), Outcome::Queued);
        assert_eq!(s.recv(0, 1, 3, 1, 0), Outcome::Queued);
    }

    #[test]
    fn newest_seq_wins_for_the_same_frame() {
        let mut s = Sim::new();
        s.frame(0);
        s.recv(0, 0, 2, 1, 0);
        s.recv(1, 0, 2, 2, 0);
        assert_eq!(s.x(1), Some(0));
        assert_eq!(s.x(2), Some(2));
    }

    #[test]
    fn bridge_restart_resyncs() {
        let mut s = Sim::new();
        s.frame(0);
        s.recv(5000, 0, 2, 1, 0);
        let mut queued = false;
        for i in 0..RESYNC_AFTER {
            queued = s.recv(i, 0, 2, 9, 0) == Outcome::Queued;
        }
        assert!(queued, "should give up on the old seq after {RESYNC_AFTER} tries");
    }

    #[test]
    fn too_far_ahead_is_rejected() {
        let mut s = Sim::new();
        s.frame(0);
        assert_eq!(s.recv(0, 0, 1 + RING as u32, 1, 0), Outcome::TooFar);
        assert_eq!(s.recv(1, 0, RING as u32, 1, 0), Outcome::Queued);
    }

    #[test]
    fn new_match_clears_old_inputs() {
        let mut s = Sim::new();
        s.frame(5000);
        s.recv(0, 4999, 5001, 1, 5000);
        s.recv(1, 5000, 5002, 1, 5000);
        // new match: frames restart. Game thread target goes back to 1.
        assert_eq!(s.x(1), Some(0), "old match's last input isn't repeated into the new one");
        assert_eq!(s.recv(2, 0, 2, 33, 0), Outcome::Queued);
        assert_eq!(s.x(2), Some(33));
    }

    #[test]
    fn falls_back_to_state_frame_if_hook_quiet() {
        let s = Shared::new();
        assert_eq!(s.next_target(1000, 41), 42);
    }

    #[test]
    fn capture_keyed_by_frame() {
        let c = Capture::new();
        let a = Inputs { stick_x: 10, buttons: 1, ..Inputs::NEUTRAL };
        let b = Inputs { cstick_y: -5, buttons: 4, ..Inputs::NEUTRAL };
        assert_eq!(c.frame(7), ([Inputs::NEUTRAL; 2], 0), "nothing captured yet");
        c.put(0, 7, a, false);
        c.put(1, 7, b, true);
        assert_eq!(c.frame(7), ([a, b], CAP_P1_VALID | CAP_P2_VALID | CAP_P2_DRIVEN));
        // frame 7 + CAPTURE_SLOTS lands in the same slot and replaces it
        c.put(0, 7 + CAPTURE_SLOTS as u32, b, false);
        assert_eq!(c.get(0, 7), None);
        assert_eq!(c.get(0, 7 + CAPTURE_SLOTS as u32), Some((b, false)));
        assert_eq!(c.frame(7).1, CAP_P2_VALID | CAP_P2_DRIVEN);
        // same frame again (pause) just overwrites
        c.put(1, 7, a, false);
        assert_eq!(c.get(1, 7), Some((a, false)));
        c.put(5, 7, a, false); // out of range port is ignored
    }

    #[test]
    fn capture_mapping() {
        assert_eq!(capture_map(2, 1), [0, 1], "default, bot on port 2");
        assert_eq!(capture_map(1, 0), [0, 1], "default, bot on port 1");
        assert_eq!(capture_map(2, 0), [1, 0], "override swaps the other port too");
        assert_eq!(capture_map(1, 1), [1, 0]);
        assert_eq!(capture_map(2, 4), [0, 4], "override to a slot outside 0..1 leaves port 1 alone");
    }

    #[test]
    fn seqlock_never_tears() {
        use std::sync::atomic::AtomicBool;
        use std::sync::Arc;
        let shared = Arc::new(Shared::new());
        let stop = Arc::new(AtomicBool::new(false));
        let w = {
            let shared = shared.clone();
            let stop = stop.clone();
            std::thread::spawn(move || {
                let mut n: u32 = 0;
                while !stop.load(Ordering::Relaxed) {
                    // every field derived from n, so a torn read is detectable
                    let b = n as i8;
                    shared.put(&Pending {
                        for_frame: n,
                        apply_at: 0,
                        inputs: Inputs { stick_x: b, stick_y: b, cstick_x: b, cstick_y: b, buttons: n },
                        ready: n,
                    });
                    n = n.wrapping_add(1);
                }
            })
        };
        let mut ok = 0;
        let mut misses = 0u64;
        let t0 = Instant::now();
        while ok < 500_000 && t0.elapsed().as_secs() < 5 {
            if let Some(p) = shared.get(0) {
                let b = p.for_frame as i8;
                assert_eq!(p.ready, p.for_frame);
                assert_eq!(p.inputs.buttons, p.for_frame);
                assert_eq!((p.inputs.stick_x, p.inputs.stick_y, p.inputs.cstick_x, p.inputs.cstick_y), (b, b, b, b));
                if p.for_frame > 0 {
                    ok += 1;
                }
            } else {
                misses += 1;
            }
        }
        stop.store(true, Ordering::Relaxed);
        w.join().unwrap();
        assert!(ok > 0, "reader never saw a write");
        println!("seqlock: {ok} clean reads, {misses} gave up (writer busy or slot empty)");
    }

    #[test]
    fn stick_float_to_i8() {
        assert_eq!(stick_to_i8(0.0), 0);
        assert_eq!(stick_to_i8(1.0), 127);
        assert_eq!(stick_to_i8(-1.0), -127);
        assert_eq!(stick_to_i8(2.5), 127);
        assert_eq!(stick_to_i8(-9.0), -128);
        assert_eq!(stick_to_i8(0.5), 64);
        assert_eq!(stick_to_i8(f32::NAN), 0);
    }

    #[test]
    fn pad_buttons_translate() {
        let held = |want: &'static [i32]| cm_buttons(|i| want.contains(&i));
        assert_eq!(held(&[]), 0);
        assert_eq!(held(&[0, 2]), 0x1 | 0x4);
        // catch is pad index 9 but MappedInputs bit 0x10
        assert_eq!(held(&[9]), 0x10);
        assert_eq!(held(&[0xA, 0x10]), 0x40 | 0x10000);
        // all distinct single bits
        let all = cm_buttons(|_| true);
        assert_eq!(all.count_ones() as usize, PAD_BUTTONS.len());
    }
}
