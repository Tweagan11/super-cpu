//! Per-frame fighter state (SSB-3). Field order and types must match
//! `bridge/bridge/protocol.py` and `docs/protocol.md`.
//!
//! Live: fighter kind, position, velocity, damage, shield, stocks, status,
//! motion, animation frame, facing, jumps left, airborne and hitstun flags,
//! and the stage id. The invincible flag is still sent as 0: the Training
//! Modpack never reads hit status, so there's no precedented accessor for it
//! yet (see docs/protocol.md).
//!
//! Every game call here follows something the Training Modpack does; the
//! comment on each one says where.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::SyncSender;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use smash::app::{self, lua_bind::*, utility, BattleObjectModuleAccessor};

use crate::input::{cm_buttons, stick_to_i8, Inputs, CAPTURE, CAP_P1_CM, CAP_P2_CM, PAD_BUTTONS};
use smash::lib::lua_const::*;

#[repr(C)]
#[derive(Clone, Copy, Default, Debug)]
pub struct FighterState {
    pub fighter_kind: i32,
    pub pos_x: f32,
    pub pos_y: f32,
    pub vel_x: f32,
    pub vel_y: f32,
    pub damage: f32,
    pub shield_hp: f32,
    pub stocks: i32,
    pub status_kind: i32,
    pub motion_kind: u64,
    pub anim_frame: f32,
    pub facing: i32,
    pub jumps_left: i32,
    /// bit 0 airborne, bit 1 in hitstun, bit 2 invincible
    pub flags: u32,
}

pub const FLAG_AIRBORNE: u32 = 1;
pub const FLAG_HITSTUN: u32 = 1 << 1;
/// Reserved, never set yet (no precedented accessor). Kept so the bit is
/// documented in one place.
#[allow(dead_code)]
pub const FLAG_INVINCIBLE: u32 = 1 << 2;

#[derive(Clone, Copy, Default, Debug)]
pub struct FrameState {
    pub frame: u32,
    pub stage_id: i32,
    /// index 0 = port 1, index 1 = port 2 (bot/opponent mapping comes later)
    pub fighters: [FighterState; 2],
    /// SSB-13: the final mapped inputs each port's fighter read this frame,
    /// filled from `input::CAPTURE` when the frame is published.
    pub inputs: [Inputs; 2],
    /// `input::CAP_*` bits: which of `inputs` are real, and which the PC drove.
    pub input_flags: u32,
    /// v4: each fighter's own ControlModule inputs this frame (CPU ports
    /// too), buttons translated to the MappedInputs layout.
    pub cm_inputs: [Inputs; 2],
    /// `CAP_P*_CM` bits for the entries read so far this frame; merged into
    /// `input_flags` when the frame is published.
    pub cm_valid: u32,
}

/// Game-thread side of the bridge. Only touched from the frame hook.
struct Collector {
    current: FrameState,
    last_entry: i32,
    last_seen: Option<Instant>,
    /// Costume slot printed once per entry per match (SSB-8 asks which slot
    /// is green R.O.B.). Log only, not in any packet.
    color_logged: [bool; 2],
}

static COLLECTOR: Mutex<Collector> = Mutex::new(Collector {
    current: FrameState { frame: 0, stage_id: 0, fighters: [FighterState {
        fighter_kind: 0, pos_x: 0.0, pos_y: 0.0, vel_x: 0.0, vel_y: 0.0, damage: 0.0,
        shield_hp: 0.0, stocks: 0, status_kind: 0, motion_kind: 0, anim_frame: 0.0,
        facing: 0, jumps_left: 0, flags: 0,
    }; 2], inputs: [Inputs::NEUTRAL; 2], input_flags: 0,
        cm_inputs: [Inputs::NEUTRAL; 2], cm_valid: 0 },
    last_entry: i32::MAX,
    last_seen: None,
    color_logged: [false; 2],
});

static SENDER: OnceLock<SyncSender<FrameState>> = OnceLock::new();
/// Frames handed to the network thread, reported in the heartbeat.
pub static FRAMES_QUEUED: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
static FIGHTER_MANAGER: AtomicUsize = AtomicUsize::new(0);
/// The last gameplay frame had a fighter out of stocks: the match is over and
/// what follows in the match scene is the victory screen. (Stocks read 0 for
/// the first frames of a match too, but fighter frames are live then.)
static LAST_ZERO_STOCKS: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// True after a stock match ended (someone at 0 stocks) and no fighter frame
/// ran for 250 ms: the victory/results screen, still in the match scene.
pub fn match_over() -> bool {
    LAST_ZERO_STOCKS.load(Ordering::Relaxed) && live_frame().is_none()
}

/// A gap this long with no fighter frames means the next frame is a new match.
const NEW_MATCH_GAP: Duration = Duration::from_secs(1);

pub fn init(sender: SyncSender<FrameState>) {
    let _ = SENDER.set(sender);
    unsafe {
        let mut addr: usize = 0;
        skyline::nn::ro::LookupSymbol(
            &mut addr,
            "_ZN3lib9SingletonIN3app14FighterManagerEE9instance_E\u{0}".as_ptr(),
        );
        FIGHTER_MANAGER.store(addr, Ordering::Relaxed);
        println!("[super-cpu] FighterManager symbol at {:#x}", addr);
    }
}

/// SSB-6 helper: the frame number being collected right now, if a match is
/// live (a fighter frame ran in the last 250 ms). Called from the input hook,
/// which runs before the fighters update, so this is the last finished frame.
/// Never blocks: None if the lock is busy.
pub fn live_frame() -> Option<u32> {
    let c = match COLLECTOR.try_lock() {
        Ok(c) => c,
        Err(std::sync::TryLockError::Poisoned(p)) => p.into_inner(),
        Err(std::sync::TryLockError::WouldBlock) => return None,
    };
    let fresh = c.last_seen.map_or(false, |t| t.elapsed() < Duration::from_millis(250));
    fresh.then_some(c.current.frame)
}

/// Called once per fighter per frame from the vtable hook in `frame.rs`.
///
/// Fighters run in entry order within a frame, so when the entry id stops
/// going up, the previous frame is complete: publish it and start a new one.
pub fn on_fighter_frame(acc: &mut BattleObjectModuleAccessor) {
    // SSB-10: wrong game build means no state at all, and we don't even
    // read game memory. Heartbeats keep going from net.rs.
    if !crate::version::game_version_ok() {
        return;
    }
    unsafe {
        if (acc.battle_object_id >> 28) as i32 != *BATTLE_OBJECT_CATEGORY_FIGHTER {
            return;
        }
        let entry = WorkModule::get_int(acc, *FIGHTER_INSTANCE_WORK_ID_INT_ENTRY_ID);
        if !(0..2).contains(&entry) {
            return;
        }
        let fighter = read_fighter(acc, entry);
        let cm = read_control_inputs(acc);
        // The results screen keeps fighters alive in DEMO (victory/clap
        // animations). Not gameplay: don't send it, and don't refresh
        // last_seen, so the next real match is detected as a new one.
        if fighter.status_kind == *FIGHTER_STATUS_KIND_DEMO {
            return;
        }

        let mut c = match COLLECTOR.lock() {
            Ok(c) => c,
            Err(poisoned) => poisoned.into_inner(),
        };
        let now = Instant::now();
        let new_match = c.last_seen.map_or(true, |t| now.duration_since(t) > NEW_MATCH_GAP);
        if new_match {
            println!("[super-cpu] match started");
            c.current = FrameState::default();
            c.last_entry = i32::MAX;
            c.color_logged = [false; 2];
        } else if entry <= c.last_entry {
            // wrapped around: the last frame is done. Its inputs were
            // captured by the input hook one game frame ago (keyed by this
            // same frame number, see input::on_final_input_mapping), so
            // they're in the ring already. Lock-free read.
            let (inputs, input_flags) = CAPTURE.frame(c.current.frame);
            c.current.inputs = inputs;
            c.current.input_flags = input_flags | c.current.cm_valid;
            if let Some(tx) = SENDER.get() {
                // never block the game; if the queue is full the frame is dropped
                if tx.try_send(c.current).is_ok() {
                    FRAMES_QUEUED.fetch_add(1, Ordering::Relaxed);
                }
            }
            c.current.frame = c.current.frame.wrapping_add(1);
            c.current.cm_inputs = [Inputs::NEUTRAL; 2];
            c.current.cm_valid = 0;
        }
        c.last_seen = Some(now);
        c.last_entry = entry;
        c.current.fighters[entry as usize] = fighter;
        LAST_ZERO_STOCKS.store(c.current.fighters.iter().any(|f| f.stocks == 0), Ordering::Relaxed);
        c.current.cm_inputs[entry as usize] = cm;
        c.current.cm_valid |= if entry == 0 { CAP_P1_CM } else { CAP_P2_CM };
        if crate::input::drive_cpu() && entry as u8 + 1 == crate::input::bot_port() {
            check_drive_agreement(entry as usize, c.current.frame, cm);
        }
        // Same global the modpack reads mid-match (save_states.rs, tech.rs).
        // The stage can't change during a match, but reading it every
        // frame is one cheap call and avoids caring about match start order.
        c.current.stage_id = app::sv_information::stage_id();

        if !c.color_logged[entry as usize] {
            c.color_logged[entry as usize] = true;
            // FIGHTER_INSTANCE_WORK_ID_INT_COLOR has no modpack precedent,
            // so it only goes to the log. Read the same way as ENTRY_ID
            // above (WorkModule::get_int on an instance work id).
            let color = WorkModule::get_int(acc, *FIGHTER_INSTANCE_WORK_ID_INT_COLOR);
            println!(
                "[super-cpu] P{} fighter_kind {} color {} (stage_id {})",
                entry + 1,
                fighter.fighter_kind,
                color,
                c.current.stage_id
            );
            // SSB-24: compare with what the force hook wrote (None unless
            // force_fighter is on and this port is in force_ports).
            if let Some(msg) = crate::force::check(entry as u8 + 1, fighter.fighter_kind, color) {
                crate::log::log(msg);
            }
        }
    }
}

/// This fighter's inputs as its ControlModule sees them (v4). Same calls the
/// modpack hooks: get_stick_x/y (mod.rs), check_button_on (mod.rs,
/// full_hop.rs); get_sub_stick_x/y are the c-stick pair next to them in
/// skyline-smash. Sticks are absolute (not flipped by facing). Runs after the
/// fighter's own control update, so these are the values it just acted on.
unsafe fn read_control_inputs(acc: &mut BattleObjectModuleAccessor) -> Inputs {
    verify_pad_indices();
    Inputs {
        stick_x: stick_to_i8(ControlModule::get_stick_x(acc)),
        stick_y: stick_to_i8(ControlModule::get_stick_y(acc)),
        cstick_x: stick_to_i8(ControlModule::get_sub_stick_x(acc)),
        cstick_y: stick_to_i8(ControlModule::get_sub_stick_y(acc)),
        buttons: cm_buttons(|idx| ControlModule::check_button_on(acc, idx)),
    }
}

/// drive_cpu cross-check: the inputs the CPU hook wrote for this frame (in the
/// capture ring, marked driven) against what the bot's ControlModule reports
/// now. Counts agreements, logs the first few disagreements and a summary
/// every 300 compared frames. Disagreement with a working walk policy means
/// the wrong CPU was driven or the stick scaling is off (docs/hooks.md 3).
fn check_drive_agreement(port: usize, frame: u32, cm: Inputs) {
    use std::sync::atomic::AtomicU32;
    static AGREE: AtomicU32 = AtomicU32::new(0);
    static DISAGREE: AtomicU32 = AtomicU32::new(0);
    let Some((applied, true)) = CAPTURE.get(port, frame) else { return };
    let ok = crate::cpu_drive::cm_agrees(applied, cm);
    let (a, d) = if ok {
        (AGREE.fetch_add(1, Ordering::Relaxed) + 1, DISAGREE.load(Ordering::Relaxed))
    } else {
        (AGREE.load(Ordering::Relaxed), DISAGREE.fetch_add(1, Ordering::Relaxed) + 1)
    };
    // first 5 since boot, then one example every 100 disagreements
    if !ok && (d <= 5 || d % 100 == 0) {
        crate::log::log(format!("drive_cpu: frame {frame} wrote {applied:?} but the control module reads {cm:?}"));
    }
    if (a + d) % 300 == 0 {
        crate::log::log(format!("drive_cpu: control module agrees with the written inputs on {a} of {} frames", a + d));
    }
}

/// Once: the pad button indices in `input::PAD_BUTTONS` are copied from
/// skyline-smash; compare them with the real `CONTROL_PAD_BUTTON_*` values
/// and log any difference (it would silently scramble CPU buttons).
fn verify_pad_indices() {
    static DONE: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
    if DONE.swap(true, Ordering::Relaxed) {
        return;
    }
    let actual: [i32; 18] = [
        *CONTROL_PAD_BUTTON_ATTACK, *CONTROL_PAD_BUTTON_SPECIAL, *CONTROL_PAD_BUTTON_JUMP,
        *CONTROL_PAD_BUTTON_GUARD, *CONTROL_PAD_BUTTON_SMASH, *CONTROL_PAD_BUTTON_APPEAL_HI,
        *CONTROL_PAD_BUTTON_APPEAL_LW, *CONTROL_PAD_BUTTON_APPEAL_S_L, *CONTROL_PAD_BUTTON_APPEAL_S_R,
        *CONTROL_PAD_BUTTON_CATCH, *CONTROL_PAD_BUTTON_JUMP_MINI, *CONTROL_PAD_BUTTON_CSTICK_ON,
        *CONTROL_PAD_BUTTON_FLICK_JUMP, *CONTROL_PAD_BUTTON_STOCK_SHARE, *CONTROL_PAD_BUTTON_ATTACK_RAW,
        *CONTROL_PAD_BUTTON_SPECIAL_RAW, *CONTROL_PAD_BUTTON_GUARD_HOLD, *CONTROL_PAD_BUTTON_SPECIAL_RAW2,
    ];
    let mut bad = 0;
    for (got, (want, _, name)) in actual.iter().zip(PAD_BUTTONS.iter()) {
        if got != want {
            bad += 1;
            crate::log::log(format!("control module: pad button {name} is {got:#x}, table says {want:#x}"));
        }
    }
    if bad == 0 {
        crate::log::log("control module: pad button table matches the game constants");
    }
}

unsafe fn read_fighter(acc: &mut BattleObjectModuleAccessor, entry: i32) -> FighterState {
    let mut flags = 0;
    if StatusModule::situation_kind(acc) == *SITUATION_KIND_AIR {
        flags |= FLAG_AIRBORNE;
    }
    let status_kind = StatusModule::status_kind(acc);
    if is_hitstun_status(status_kind) {
        flags |= FLAG_HITSTUN;
    }

    // Velocity: KineticModule::get_sum_speed_x/_y, as in the modpack's
    // fast_fall.rs (is_falling). The modpack passes the value 1 there
    // (spelled FIGHTER_KINETIC_ENERGY_ID_GRAVITY); 1 is also
    // KINETIC_ENERGY_RESERVE_ATTRIBUTE_MAIN, which is what the skyline-smash
    // docs for get_sum_speed_x/_y use for "current velocity". Same value,
    // clearer name.
    let vel_x = KineticModule::get_sum_speed_x(acc, *KINETIC_ENERGY_RESERVE_ATTRIBUTE_MAIN);
    let vel_y = KineticModule::get_sum_speed_y(acc, *KINETIC_ENERGY_RESERVE_ATTRIBUTE_MAIN);

    // Shield and jumps are instance work variables, read with the same
    // WorkModule::get_float/get_int calls the modpack uses for its work
    // reads (common/mod.rs and save_states.rs for ENTRY_ID, charge.rs and
    // debug.rs::print_fighter_info for floats and other ints). The ids
    // themselves come from skyline-smash lua_const.rs.
    let shield_hp = WorkModule::get_float(acc, *FIGHTER_INSTANCE_WORK_ID_FLOAT_GUARD_SHIELD);
    let jump_max = WorkModule::get_int(acc, *FIGHTER_INSTANCE_WORK_ID_INT_JUMP_COUNT_MAX);
    let jumps_used = WorkModule::get_int(acc, *FIGHTER_INSTANCE_WORK_ID_INT_JUMP_COUNT);

    FighterState {
        fighter_kind: utility::get_kind(acc),
        pos_x: PostureModule::pos_x(acc),
        pos_y: PostureModule::pos_y(acc),
        vel_x,
        vel_y,
        damage: DamageModule::damage(acc, 0),
        shield_hp,
        stocks: stock_count(entry),
        status_kind,
        motion_kind: MotionModule::motion_kind(acc),
        anim_frame: MotionModule::frame(acc),
        facing: if PostureModule::lr(acc) < 0.0 { -1 } else { 1 },
        jumps_left: jumps_left(jump_max, jumps_used),
        flags,
    }
}

/// Jumps remaining, never negative (the count can briefly read past the max
/// on some characters and states).
pub fn jumps_left(max: i32, used: i32) -> i32 {
    (max - used).max(0)
}

/// Copied from the modpack's `is_in_hitstun` (src/common/mod.rs): any status
/// from DAMAGE up to, but not including, DAMAGE_FALL. Tumble (DAMAGE_FALL)
/// is left out on purpose, same as the modpack.
pub fn is_hitstun_status(status_kind: i32) -> bool {
    (*FIGHTER_STATUS_KIND_DAMAGE..*FIGHTER_STATUS_KIND_DAMAGE_FALL).contains(&status_kind)
}

unsafe fn stock_count(entry: i32) -> i32 {
    let addr = FIGHTER_MANAGER.load(Ordering::Relaxed);
    if addr == 0 {
        return -1;
    }
    let mgr = *(addr as *mut *mut app::FighterManager);
    if mgr.is_null() {
        return -1;
    }
    let info = FighterManager::get_fighter_information(mgr, app::FighterEntryID(entry));
    if info.is_null() {
        return -1;
    }
    FighterInformation::stock_count(info) as i32
}
