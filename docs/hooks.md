# Hook points (SSB-2)

Every place the plugin hooks the game, why, where it came from, and what we still don't know. The Training Modpack (UltimateTraining/UltimateTrainingModpack) is the reference for all of them; file paths below are in that repo.

Rule: no raw addresses. A hook is either by symbol (skyline-smash binding) or by byte-pattern search (`plugin/src/offsets.rs`, same approach as the modpack's `src/common/offsets.rs`). If a pattern isn't found after a game update, that feature turns itself off and the game still runs.

## 1. Per-fighter frame: `L2CFighterCommon::sys_line_system_control_fighter`

| | |
| --- | --- |
| Code | `plugin/src/frame.rs` |
| How | `#[skyline::hook(replace = smash::lua2cpp::L2CFighterCommon_sys_line_system_control_fighter)]`, installed when the `common` NRO loads (needs `libnro_hook.nro`) |
| Runs | once per fighter per game frame, in entry order |
| Used for | reading fighter state (`state.rs`); the wraparound in entry id marks the end of a frame, which is where the frame counter comes from |
| Modpack precedent | the modpack hooks common-module functions the same way (`nro_main` in `src/lib.rs`). For its own per-frame hook it patches the fighter vtable at a hardcoded address (`OFFSET_FIGHTER_VTABLE_ON_FRAME` in `src/common/offsets.rs`, no needle yet), which is why we don't copy that one |
| Status | verified on hardware at 60 fps |

## 2. Final input mapping (`OFFSET_FIM`): how the bot gets its inputs

| | |
| --- | --- |
| Code | `plugin/src/control.rs` (hook), `plugin/src/input.rs` (all the logic) |
| How | byte-pattern search with `NEEDLE_FIM`, copied from `src/common/offsets.rs` (the modpack notes it was at text+0x1750dd0 when they wrote it). Hook signature copied from `handle_final_input_mapping` in `src/training/mod.rs` |
| Signature | `(mappings: *mut ControllerMapping, player_idx: i32, out: *mut MappedInputs, controller_struct: &mut SomeControllerStruct, arg: bool)`. We pass the two struct pointers through untouched, so they're `*mut u8` on our side (same ABI) |
| Runs | once per player per frame, turns that player's controller state plus their button config into `MappedInputs` |
| What we do | call the original, then if `player_idx` is the bot's and a PC session is live and a match is running, overwrite `*out`. For the two captured ports, also copy the final `*out` into the capture ring (SSB-13, docs/protocol.md "Captured inputs"), keyed by the frame it takes effect on |
| `MappedInputs` | `src/common/input.rs`: `buttons: u32` (the `Buttons` bitflags, same as `bridge/bridge/buttons.py`), then `lstick_x, lstick_y, rstick_x, rstick_y: i8`. 8 bytes, checked with a compile-time size assert |
| Modpack precedent | input delay (`src/training/input_delay.rs`) and input recording (`src/training/input_record.rs`, `handle_final_input_mapping`) both edit `*out` exactly like this, for player 0 |

## 3. CPU controls (`OFFSET_SET_CPU_CONTROLS`): read-only probe for now

| | |
| --- | --- |
| Code | `plugin/src/control.rs` (`set_cpu_controls`) |
| How | byte-pattern search with `NEEDLE_SET_CPU_CONTROLS` from `src/common/offsets.rs` (text+0x2da180 in the modpack's build). Signature from `set_cpu_controls` in `src/training/input_record.rs`: `(p_data: *mut *mut u8)` |
| Runs | after the CPU AI writes a CPU's controls for the frame |
| What we do | only read `(*p_data.add(1)).controller_index` and report which values show up (`cpu_idx_seen` in the control status packet). Changes nothing. `cpu_probe = 0` in config.toml turns it off |
| Modpack precedent | input playback puppets the CPU here by writing `buttons`, `stick_x/y` (floats) and `clamped_lstick_x/y` into `ControlModuleInternal` (`src/common/input.rs`) |

## Why final input mapping and not the CPU hook

Taking over a CPU slot would be nicer (no second controller), so we looked at it first. It's not ready:

1. **We can't tell which CPU it is.** The modpack reads `controller_index` and throws it away (`let _controller_no = ...`). It finds "the CPU" by assuming entry id 1 in training mode (`FighterId::CPU`), and the whole hook returns early outside training mode. We need versus mode against a level 9 CPU on another port, so that assumption doesn't hold.
2. **The data layout is only half known.** `ControlModuleInternal` has `padding` and `unk: [u32; 8]` in the middle. Playback writes the main stick (raw and clamped) and buttons, but never the c-stick, so we don't know where c-stick input goes for a CPU. Sticks are floats there with a 1/120 clamp and a 0.2 deadzone that the modpack picked by hand, so our i8 sticks would need a conversion that isn't verified.
3. **Never tested outside training mode.** All the modpack's evidence is training mode.

Final input mapping has none of these problems: the layout is fully known and used by four modpack features, the values are exactly our packet's (i8 sticks, mapped button bits, no conversion), and it's the same thing a human's controller produces, so data logged from human play (SSB-9) and the bot's actions live in the same space.

The cost: the bot's port needs a controller assigned to it (any controller, it just has to exist so the game builds inputs for that port). Plug in a second controller or a single Joy-Con and pick the bot's character with it. While the bridge is running that controller does nothing; stop the bridge and it works again within 2 s.

The probe is there so that switching later is cheap: one hardware session tells us what `controller_index` looks like for CPUs.

## How the frame number lines up

`state.rs` numbers frames by counting fighter-hook wraparounds. The input hook asks `state::live_frame()` for the last finished frame and looks up the input for `live_frame + 1`. That's right whether the game maps inputs before or after the fighters update within a frame: either way, `live_frame + 1` is the next frame whose fighter update reads what we write. If no fighter frame ran in the last 250 ms (menus, pause, results), `live_frame()` is None and the hook leaves the input alone.

## Open questions only hardware can answer

| Question | Why it matters | How to check |
| --- | --- | --- |
| Is `player_idx` the port or the controller slot? The modpack's own comment: "Is this the player index, or plugged in controller index? Need to check, assuming player for now - is this 0 indexed or 1?" | We assume port, 0-based, so `bot_port = 2` means `player_idx == 1`. If it's the controller slot, the bot follows whichever controller is connected second, not port 2 | `docs/testing-phase0.md` step 9c: put the controllers in a different order than the ports (first controller on P2) and see which fighter moves. If the wrong one moves, set `fim_player_idx` in config.toml. `bridge.run` prints `input player_idx seen [...]` and the game's own stick values for the bot's controller (`orig`) so you can see it with the bridge stopped. **Answered 2026-10-16: it is the port.** Controller 2 on port 1 left the bot on port 2 |
| Is the hook called for a port with no controller (a CPU)? | If yes, the bot might be able to drive a CPU port through this hook too, which would remove the extra controller | Set `bot_port` to the CPU's port and run the walk policy. If the CPU walks toward you, yes. `idx_seen` shows every `player_idx` the hook ever saw |
| Does `player_idx == -1` happen? | The function's 7th instruction is `cmn w1, #1` (compare player_idx with -1), so there's a special case. We never match -1 unless configured | `idx_seen` bit 31 ("other") |
| Is the mapped input used the same frame or buffered? | If the game buffers a frame, every input lands one frame later than `applied_frame` says, and the measured round trip is one frame low | Hold the stick right with a known `apply_at` (latency tool prints applied frames) and look for the first state frame with WALK and pos_x moving. Rough check is fine; it's a constant |
| Does FIM run on the same thread as the fighter hook? | `state::live_frame()` uses `try_lock` so the input hook never waits. If they're on different threads and collide, that one frame goes to the real controller | Watch for `neutral` or stray frames in the status line with a steady bridge. If it shows up, move the frame counter into an atomic |
| What `controller_index` values do CPUs get? | Decides whether we can take over a CPU slot later | `bridge.run` prints `cpu controller_index seen [...]` every 30 s. Try with one CPU on port 2, then port 3 |
| Do the needles still match on 13.0.4? | The modpack's offsets comments are older than its latest release, but its CI ships against current versions | Boot log (`cargo skyline listen`) says `found FIM at text+0x...`; the control status says `input hook installed` |
