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

## 3. CPU controls (`OFFSET_SET_CPU_CONTROLS`): probe, and `drive_cpu`

`drive_cpu = true` (config.toml, default off) makes the bot's port a CPU that the PC drives (written up in the last part of this section). Without it this hook is the read-only probe below.

| | |
| --- | --- |
| Code | `plugin/src/control.rs` (`set_cpu_controls`), `plugin/src/cpu_drive.rs` (layout, conversion, which-CPU logic, host tested) |
| How | byte-pattern search with `NEEDLE_SET_CPU_CONTROLS` from `src/common/offsets.rs` (text+0x2da180 in the modpack's build). Signature from `set_cpu_controls` in `src/training/input_record.rs`: `(p_data: *mut *mut u8)` |
| Runs | after the CPU AI writes a CPU's controls for the frame |
| What we do | only read `(*p_data.add(1)).controller_index` and report which values show up (`cpu_idx_seen` in the control status packet). Changes nothing. `cpu_probe = 0` in config.toml turns it off |
| Modpack precedent | input playback puppets the CPU here by writing `buttons`, `stick_x/y` (floats) and `clamped_lstick_x/y` into `ControlModuleInternal` (`src/common/input.rs`) |

### drive_cpu: the PC drives a CPU slot

Why: human cards on the character select screen are cleared after every match, so unattended rematches can't start the next one. CPU cards keep their pick (Random, which `force_*.rs` turns into green R.O.B.), so the bot's port is a CPU card and the PC replaces the AI's decision every frame.

- **Same pipeline.** `input::on_cpu_drive` runs the same `Applier` as the final input mapping path (pending inputs by `apply_at`, late/repeat/neutral rules, acks) through the shared `apply_bot`. The result goes into the capture ring marked driven, so the state packet's `driven` flags, the logger's ack/FIM/control-module preference and the acks all keep working. With `drive_cpu` on, the FIM hook does nothing for the bot's port (no apply, no capture), it still captures the other port.
- **Writes** (`cpu_drive.rs`, `ControlModuleInternal::write`): `buttons` as-is; `stick_x/y = i8/127`; `clamped_lstick_x/y = clamp(i8/120, -1, 1)` zeroed under 0.2; `clamped_rstick_x/y` likewise from the c-stick. Constants and formulas from the modpack's playback (`STICK_CLAMP_MULTIPLIER = 1/120`, `STICK_NEUTRAL = 0.2`). With no PC session the AI's decision is left alone (and logged as not driven).
- **Button layout.** The modpack's `Buttons` bitflags (`ControlModuleInternal.buttons`) and `MappedInputs.buttons` are the same type, so our bits from `bridge/buttons.py` go in unchanged. A host test checks that all 18 bits match the modpack's constants. (The ControlModule's `check_button_on` indices are a different numbering, see `input::PAD_BUTTONS`; that is only for reading.)
- **c-stick: uncertain.** The modpack never writes `clamped_rstick_x/y`; it only reads them back as the c-stick of a `MappedInputs` in `is_input_neutral`. Putting the c-stick there is a best guess. Smash attacks from the c-stick may also need the `SMASH`/`CSTICK_ON` button bits, which we pass through from the policy untouched. Step 14 of `docs/testing-phase0.md` checks it.
- **Which CPU is the bot.** `*p_data.add(1)` is the `ControlModuleInternal` of the CPU the call is for, and its `controller_index` is the only identity we get there (the fighter's `ControlModule` pointer isn't reachable through skyline-smash, and guessing offsets into `BattleObjectModuleAccessor` isn't worth a crash). `cpu_drive::CpuTracker` collects the distinct `controller_index` values the hook sees in a frame and uses the complete set of the *previous* frame: one distinct index means that is the bot; several (CPU vs CPU) are sorted ascending and the bot is rank `bot_port - 1` (assumes indices rise with the port). `cpu_controller_index = N` in config.toml overrides it. The first frame of a match is not driven (the set isn't complete yet). The decision is logged once: `drive_cpu: P2 is controller_index N (indices seen last frame 0x...)`. Hardware must confirm that line, and the cross-check below.
- **Cross-check.** After each fighter update, `state.rs` compares what was written (from the capture ring) with what the bot's own ControlModule reports (`check_drive_agreement`), logging the first mismatches and `control module agrees with the written inputs on X of Y frames` every 300 frames. Persistent disagreement means the wrong CPU, or scaling/c-stick layout is off.
- **Frame number.** The target frame is `state::live_frame()` itself, not `+ 1` like the FIM path. The CPU hook runs inside the fighter update for that frame, so what it writes is read on that same frame. Hardware 2026-10-16: with `+ 1`, the control module on frame N always read what was filed for N+1, and inputs counted as late one frame too soon (about 20% late at `--delay 3`).
- **Hooks.** `drive_cpu` installs this hook even with `cpu_probe = 0`; the FIM hook stays installed (captures the other port). `FLAG_DRIVE_CPU` (status flags bit 5) is set.

## Why final input mapping and not the CPU hook (the original default)

This was the reasoning before `drive_cpu` existed. Points 1 and 2 are now handled as described above (identity by `controller_index`, c-stick as a documented guess); point 3 is what step 14 tests. The controller route stays the default because it's the verified one.

Taking over a CPU slot would be nicer (no second controller), so we looked at it first. It's not ready:

1. **We can't tell which CPU it is.** The modpack reads `controller_index` and throws it away (`let _controller_no = ...`). It finds "the CPU" by assuming entry id 1 in training mode (`FighterId::CPU`), and the whole hook returns early outside training mode. We need versus mode against a level 9 CPU on another port, so that assumption doesn't hold.
2. **The data layout is only half known.** `ControlModuleInternal` has `padding` and `unk: [u32; 8]` in the middle. Playback writes the main stick (raw and clamped) and buttons, but never the c-stick, so we don't know where c-stick input goes for a CPU. Sticks are floats there with a 1/120 clamp and a 0.2 deadzone that the modpack picked by hand, so our i8 sticks would need a conversion that isn't verified.
3. **Never tested outside training mode.** All the modpack's evidence is training mode.

Final input mapping has none of these problems: the layout is fully known and used by four modpack features, the values are exactly our packet's (i8 sticks, mapped button bits, no conversion), and it's the same thing a human's controller produces, so data logged from human play (SSB-9) and the bot's actions live in the same space.

The cost: the bot's port needs a controller assigned to it (any controller, it just has to exist so the game builds inputs for that port). Plug in a second controller or a single Joy-Con and pick the bot's character with it. While the bridge is running that controller does nothing; stop the bridge and it works again within 2 s.

The probe is there so that switching later is cheap: one hardware session tells us what `controller_index` looks like for CPUs.

## How the frame number lines up

`state.rs` numbers frames by counting fighter-hook wraparounds. The input hook asks `state::live_frame()` for the last finished frame and looks up the input for `live_frame + 1`. That's right whether the game maps inputs before or after the fighters update within a frame: either way, `live_frame + 1` is the next frame whose fighter update reads what we write. If no fighter frame ran in the last 250 ms (menus, pause, results), `live_frame()` is None and the hook leaves the input alone.

## 5. Scene reporting and menu presses (SSB-25)

Two more hooks, both fixed `.text` offsets for 13.0.4 from `docs/css-research.md` (HDR-1304 and ARCropolis). They are only installed when the running game version is 13.0.4, and are not byte-pattern searched, so a different build gets neither. Code: `plugin/src/menu_hook.rs`, logic in `scene.rs` and `menu.rs`.

| Hook | Offset | What it does |
| --- | --- | --- |
| Scene change | `0x3726120` | Read only. Reads the target scene's name (NUL terminated string at `target + 8`, at most 32 bytes), keeps it for the scene packet (kind 7) and logs `scene #n: "old" -> "new"` through `log::log`. Always installed |
| Controller update | `0x36650c0` (HDR `process_inputs_handheld`) | After the original ran, ORs the current menu press into the bot's `Controller` (`current_buttons` and `just_down`). Installed only with `auto_menu = true` |

The press is applied only when all of these hold: the scene is known and not the match (name contains "melee" and not "result", `scene::is_match_scene`), no fighter frame ran in the last 250 ms (`state::live_frame()`), and the controller's `npad_number` is `bot_port - 1`. The first eight distinct (style, npad, controller_id) combos the hook sees are logged as `controller update: style S npad N id I`.

`is_result_mode` is not read: the PC tells results apart by scene name. Reading it would mean calling a game function from another thread or adding game-thread work, for a signal the scene already gives. Add it if E1 shows the results screen has no scene of its own.

Risks, in the order I'd expect them to bite:

- **The prototype of `0x36650c0` is unverified.** The doc names the offset and "the controller update", not the signature. The hook takes `(controller, a1, a2, a3)` and forwards all four and the return value, so extra arguments survive, but if the first argument isn't the `Controller` the plugin reads garbage and could crash on the first controller update. It's behind `auto_menu`, off by default. If a boot with `auto_menu = true` crashes, that's the first suspect.
- **One function may not cover every controller style.** HDR calls it the handheld path. A Pro Controller or GC adapter may go through other update functions, and then presses never land. Step 11c of `testing-phase0.md` checks with the logged style list.
- **Which controller gets presses.** Not the bot's port anymore: with drive_cpu the bot is a CPU card with no controller, so presses meant for `npad_number == bot_port - 1` went nowhere (seen on hardware 2026-10-16: no press landed on results). Presses now go to `menu_npad` from config.toml, or by default the lowest npad seen with a nonzero style (connected), logged as `menu presses go to npad N`. Any controller can advance results, pick the hovered stage and start from the CSS.
- **`Controller` layout.** Copied from the Training Modpack (size checked at compile time, 0xd0). A wrong offset writes into the wrong field.
- **Scene hook signature.** Same story: forwarded untouched, only the second argument is read, as a string pointer with a null check. The 8 byte header before the name is assumed (HDR and ARCropolis disagree on its contents, not its size).
- **Wrong scene names.** The match and results names are guesses (E1). A wrong match guess could allow a press during a match (the fighter-frame check is the backstop) or block presses on results (`bridge.run` logs every scene it sees and says when it won't press).
- **Other mods.** ARCropolis and HDR hook the same functions. Skyline hooks chain, but the order depends on load order.
- **Licenses.** HDR is AGPL and ARCropolis GPL: offsets and struct layouts only, no code copied.

## 6. Forced fighter: green R.O.B. without a cursor (SSB-24)

Two more fixed `.text` offsets for 13.0.4 from `docs/css-research.md` (HDR-1304 `chara_select/random.rs`). Only installed on 13.0.4 and only with `force_fighter = true`. Code: `plugin/src/force_hook.rs`, decisions in `force.rs`.

| Hook | Offset | What it does |
| --- | --- | --- |
| Set random fighter data | `0x1a0d540` | `base` is the per-player decided-fighter record. `*base as u8` is the 1-based port (HDR: `(*base as u8) - 1` is the player id), word 3 (`base[3]`) holds the ui_chara hash in its low 40 bits, the byte at `base + 0x20` is the costume. For a port in `force_ports`: `base[3] = hash40("ui_chara_robot") \| (base[3] & KEY_MASK)` and `costume = force_costume`, written before and after the original, as HDR does |
| Record copy (inline) | `0x1798ac8` | HDR copies words 2 to 4 from `x0` to `x1` here so the choice reaches the match. We write the same two fields into the source record (`x0`) before the copy runs. Skip it with `force_copy_hook = false` |

Config (all in `config.toml`, template has them commented out):

```toml
force_fighter = true      # default false
force_ports = [2]         # 1-based; "2,3" also works; include the opponent CPU's port to force it too
force_costume = 5         # 0 to 15, green R.O.B. is 5
force_copy_hook = true    # default true
```

Only the hash (keeping the top 24 bits of the word) and the costume byte are written; everything else in the record is left alone. Pointers are null and alignment checked and the port byte has to be a forced one.

**Logging** (all `[switch]` lines on the PC):

- `force pre|post|copy: P2 saw hash 0x... c03 raw w0 .. w2 .. w4 ..; wrote hash 0x0e18857219 c05` on every fire for the first 12 fires per port, then only when what was seen changes. This is the E5 answer: does it fire, and what does a CPU card (or Random) look like.
- `force pre|post|copy dump P2 before: <0x30 bytes hex>` the first 4 times per phase, before our write.
- `force check: P2 robot c05 OK` once per fighter when a match starts. `MISMATCH want robot c05, got kind 12 c03` if the fighter or costume differs (`fighter_kind` 0x2D is R.O.B.; costume is `FIGHTER_INSTANCE_WORK_ID_INT_COLOR`). `NOT FORCED, hook never fired for it before this match` if the hook didn't run for that port since the last check. The in-match check covers ports 1 and 2 only, the two entries the state reader sees.

Risks, in the order I'd expect them to bite:

- **Prototype of `0x1a0d540` is unverified.** The doc gives the record layout, not the signature. The hook takes `(base, a1, a2, a3)` and forwards all four and the return value. If the first argument isn't the record pointer, the port byte won't match a forced port and nothing is written, which shows up as no `force` lines at all.
- **Hash word 2 vs 3.** The doc's table calls `base[2]` the main chara key and `base[3]` the sub key, but its recipe (which we follow) writes `base[3]`. If `force check` says MISMATCH with the hash sitting in `w2` in the dump, the main key is the one that counts. The dump has the evidence either way.
- **Does it fire for a CPU card?** E5. Unknown whether the function runs for a card that isn't on Random, or runs at all for a CPU. A non-Random pick may be decided elsewhere. Step 12 tests all three.
- **Copy hook semantics.** We assume `x0` is the source and has the same layout as `base`. If the copy hook misfires, `force_copy_hook = false` removes it. It is an inline hook, so a bad address would crash at boot, not at match time.
- **Cosmetic.** The CSS or results screen may still show Random or the old portrait (the reason HDR needed the copy hook).
- **Other mods.** HDR and ARCropolis hook nearby functions. Skyline chains hooks but order follows load order.
- **Licenses.** HDR is AGPL and ARCropolis GPL: offsets and struct layouts only, no code copied.

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

## UDP receive on the Switch (2026-10-16, found on hardware)

- `UdpSocket::try_clone` fails (os error 1), so the receive thread shares the send socket through an `Arc` instead.
- A socket bound to `0.0.0.0:0` has no usable port until it has sent something: `recv_from` before the first send fails with os error 1. The receive thread starts after the first heartbeat goes out, and that fixed it.
- One thread blocking in `recv_from` (20 ms read timeout) while another sends on the same socket works. One state send took 4.05 ms in the first session; the plugin now reports every 10 s how many sends went over 4 ms, to tell Wi-Fi jitter apart from sends waiting on the receive.

## Latency on hardware (SSB-7, 2026-10-16, Wi-Fi)

`bridge.latency`, 60 s: round trip median 2 frames, p99 2, max 3, 0 late, 0 lost, 0 of 7433 state packets lost. An earlier run had about 13 stalls of 8+ frames per minute, one per 5 s; they were the plugin resending its log lines in bursts, and went away once that stopped during play. So `bridge.run` defaults to `--delay 3` and training to `--max-delay 4` (one frame of margin on each).

## Hardware results, 2026-10-16 (13.0.4)

- **drive_cpu works in versus**: with port 2 a level 9 CPU card and `drive_cpu = true`, `bridge.run --policy walk` took over the CPU and made it walk. `set_cpu_controls` is called outside training mode.
- **force_fighter works**: Random on a CPU card becomes green R.O.B. (c05); a fixed pick is left alone. The decided-fighter record matches the research layout: the main ui_chara hash is word 3 (low 40 bits, e.g. `0x0e18857219` robot), word 2 holds another hash (`0x0fd5f7fa78`, unchanged), costume is the byte at `+0x20`. The record is written twice per match start ("pre" then "post"): Random re-rolls in between (seen `0x0d61668319 c06`) and the post write puts R.O.B. c05 back. A record already holding R.O.B. c05 is left as is ("already forced").

## Ice Climbers (2026-10-19)

Nana is a second fighter object with Popo's entry id. The state collector
treated her as the next frame starting (entry 0 after entry 0), so with
Ice Climbers on either port every game frame was published twice and the
port's state alternated between Popo and Nana. Found from an RL run where
four Popo matches logged 3,000 to 46,000% damage dealt with 1,500 to 4,100
damage drops and no KO. `state::on_fighter_frame` now skips
`FIGHTER_KIND_NANA`, so the port is Popo. Logs from before the fix with
Ice Climbers in them have doubled frame counts and mixed states: drop them
from training (`python -m training.dagger_report <dirs> --bad-damage` lists
them).
