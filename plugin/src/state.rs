//! Per-frame fighter state (SSB-3). Field order and types must match
//! `bridge/bridge/protocol.py` and `docs/protocol.md`.
//!
//! First cut: fighter kind, position, damage, stocks, status, motion,
//! animation frame, facing and airborne are live. Velocity, shield, jumps
//! and the other flags are sent as 0 until they're wired up.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::SyncSender;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use smash::app::{self, lua_bind::*, utility, BattleObjectModuleAccessor};
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

#[derive(Clone, Copy, Default, Debug)]
pub struct FrameState {
    pub frame: u32,
    pub stage_id: i32,
    /// index 0 = port 1, index 1 = port 2 (bot/opponent mapping comes later)
    pub fighters: [FighterState; 2],
}

/// Game-thread side of the bridge. Only touched from the frame hook.
struct Collector {
    current: FrameState,
    last_entry: i32,
    last_seen: Option<Instant>,
}

static COLLECTOR: Mutex<Collector> = Mutex::new(Collector {
    current: FrameState { frame: 0, stage_id: 0, fighters: [FighterState {
        fighter_kind: 0, pos_x: 0.0, pos_y: 0.0, vel_x: 0.0, vel_y: 0.0, damage: 0.0,
        shield_hp: 0.0, stocks: 0, status_kind: 0, motion_kind: 0, anim_frame: 0.0,
        facing: 0, jumps_left: 0, flags: 0,
    }; 2] },
    last_entry: i32::MAX,
    last_seen: None,
});

static SENDER: OnceLock<SyncSender<FrameState>> = OnceLock::new();
static FIGHTER_MANAGER: AtomicUsize = AtomicUsize::new(0);

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

/// Called once per fighter per frame from the vtable hook in `frame.rs`.
///
/// Fighters run in entry order within a frame, so when the entry id stops
/// going up, the previous frame is complete: publish it and start a new one.
pub fn on_fighter_frame(acc: &mut BattleObjectModuleAccessor) {
    unsafe {
        if (acc.battle_object_id >> 28) as i32 != *BATTLE_OBJECT_CATEGORY_FIGHTER {
            return;
        }
        let entry = WorkModule::get_int(acc, *FIGHTER_INSTANCE_WORK_ID_INT_ENTRY_ID);
        if !(0..2).contains(&entry) {
            return;
        }
        let fighter = read_fighter(acc, entry);

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
        } else if entry <= c.last_entry {
            // wrapped around: the last frame is done
            if let Some(tx) = SENDER.get() {
                let _ = tx.try_send(c.current); // never block the game
            }
            c.current.frame = c.current.frame.wrapping_add(1);
        }
        c.last_seen = Some(now);
        c.last_entry = entry;
        c.current.fighters[entry as usize] = fighter;
    }
}

unsafe fn read_fighter(acc: &mut BattleObjectModuleAccessor, entry: i32) -> FighterState {
    let mut flags = 0;
    if StatusModule::situation_kind(acc) == *SITUATION_KIND_AIR {
        flags |= FLAG_AIRBORNE;
    }
    FighterState {
        fighter_kind: utility::get_kind(acc),
        pos_x: PostureModule::pos_x(acc),
        pos_y: PostureModule::pos_y(acc),
        damage: DamageModule::damage(acc, 0),
        stocks: stock_count(entry),
        status_kind: StatusModule::status_kind(acc),
        motion_kind: MotionModule::motion_kind(acc),
        anim_frame: MotionModule::frame(acc),
        facing: if PostureModule::lr(acc) < 0.0 { -1 } else { 1 },
        flags,
        ..FighterState::default()
    }
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
