# CSS and scene research (SSB-23 / KAN-31)

What it takes to have the plugin pick green R.O.B. on the character select screen (CSS) on any roster, and to know which scene the game is on so rematches can be automated. Research only: no plugin code changed.

Everything below is for **13.0.4**, the version `plugin/src/version.rs` pins. Each finding is tagged:

- **[source]**: I read it in a public repo, linked inline.
- **[inferred]**: my reading of that source. Plausible, not checked.
- **[unknown]**: needs hardware (see Experiments).

The repos were read at these commits. Line numbers are against them.

| Short name | Repo @ commit | Game version it targets |
| --- | --- | --- |
| HDR-1304 | [HewDraw-Remix @ c8f027d](https://github.com/HDR-Development/HewDraw-Remix/tree/c8f027d502a42eb67a426187921beadff983c3ab) (the last commit before the 13.0.5 port `ea1eb702`) | 13.0.4 |
| HDR | [HewDraw-Remix @ a8f98ea](https://github.com/HDR-Development/HewDraw-Remix/tree/a8f98eace1c6a7b45c88cb3f0ac1ef05426624dc) (current) | 13.0.5 |
| ARC | [ARCropolis @ bf2005b](https://github.com/Raytwo/ARCropolis/tree/bf2005b0282a524f242ae8977a70dad23e801318) | 13.0.5 only (the boot check refuses anything else, [lib.rs:516](https://github.com/Raytwo/ARCropolis/blob/bf2005b0282a524f242ae8977a70dad23e801318/src/lib.rs#L516)) |
| UTM | [UltimateTrainingModpack @ 077a8e4](https://github.com/jugeeya/UltimateTrainingModpack/tree/077a8e43b11d1f61ce57169156fb82e92e784c79) (local clone) | byte patterns, version agnostic |
| smash | [skyline-smash @ d2ca90d](https://github.com/ultimate-research/skyline-smash/tree/d2ca90d9ecffdf9bf58efe6bcf4dd49f62ea1435) | symbols |

Offsets below are `.text` offsets, the number you give `#[skyline::hook(offset = ...)]`. ARCropolis's arcadia tables use Ghidra addresses, which are the same number plus `0x7100000000`.

## TL;DR

- **Scene detection is solved.** Two independent sources hook the same scene-push function at `0x3726120` on 13.0.4, and both get the scene name as a string (`"CharaSelectScene"`, `"StageSelectScene"`, ...). For the results screen, add `FighterManager::is_result_mode` (symbol based).
- **Auto-select can skip cursor driving entirely.** HDR forces a fighter and costume onto a player who picked Random. It writes a `ui_chara` hash and a costume byte into the per-player "decided fighter" record at match start (`0x1a0d540`). If the bot's card sits on Random, the plugin can turn it into `ui_chara_robot` with costume 5, and the roster layout stops mattering.
- **Menus don't read the input the bot uses in matches.** Matches use `MappedInputs` (after the button config). Menus read the raw `Controller` struct: `current_buttons` and `just_down`. UTM and HDR both press menu buttons by writing that struct. So driving menus needs a second, controller-level hook.
- **Costume index:** `FIGHTER_INSTANCE_WORK_ID_INT_COLOR` is the 0-based slot, the same number as the CSS color index (c00 = 0). Community lists put green R.O.B. 6th, so **index 5 (c05)**. Medium confidence. The log line the plugin already prints settles it.
- **Not found:** CPU level, the cursor position, and which plugin prints `CharacterInfo { color_start_index ... }`. All three need hardware or the user's SD card.

## 1. Roster data (ui_chara_db)

**File structure [source].** `ui/param/database/ui_chara_db.prc` is a `db_root` list with one struct per `ui_chara`. These field names are labeled hashes in [param-labels](https://github.com/ultimate-research/param-labels/blob/85f2ecf15cfaaeb09f623997a1430345f3b5adfa/ParamLabels.csv): `ui_chara_id`, `fighter_kind`, `disp_order`, `color_num`, `c00_index`, `n00_index`, `can_select`, `is_mii`, `exhibit_year`, `ui_series_id`, `alt_chara_id`, `is_dlc`. A vanilla patch file in [FighterSort](https://github.com/Mode8fx/FighterSort/blob/a5e65dc89e77792c49f56b753926a7dd38145d47/ui_chara_db%20(original).prcxml) shows R.O.B. at list index 48 with `color_num` 8, `n00_index`..`n07_index`, and `characall_label_c00..c07`. That file only lists the fields it patches, not every field. `hash40("ui_chara_robot") = 0x0e18857219` (checked against param-labels line 77111).

**Reading the loaded DB at runtime: three ways.**

1. **The game's own parameter DB [source].** ARCropolis reads it like this ([fixes.rs:85-124](https://github.com/Raytwo/ARCropolis/blob/bf2005b0282a524f242ae8977a70dad23e801318/src/fixes.rs#L85)):
   - A global `ParametersCache*` is found with a byte pattern plus ADRP/LDR decoding (`PARAMETERS_CACHE_SEARCH_CODE`, [offsets.rs:236](https://github.com/Raytwo/ARCropolis/blob/bf2005b0282a524f242ae8977a70dad23e801318/src/offsets.rs#L236)).
   - From there, `cache->databases->character` sits at offset 360 (0x168).
   - The game functions `get_color_num_from_hash(db, ui_chara_hash) -> u8` and `get_ui_chara_echo(db, ui_chara_hash)` query it.
   - Because ARCropolis swaps in mod files when they load, this is the DB as modded **[inferred]**.
   - Watch out: `get_color_num_from_hash` is hardcoded at `0x3238A70`, which is the 13.0.5 value, or a stale one. On 13.0.4, find it with a pattern or in Ghidra.
2. **The file through the ARCropolis API, parsed with prc-rs [source].** HDR does this for `ui_stage_db.prc` ([utils/src/modules/param.rs:502](https://github.com/HDR-Development/HewDraw-Remix/blob/a8f98eace1c6a7b45c88cb3f0ac1ef05426624dc/utils/src/modules/param.rs#L502)):
   - Register `#[arc_callback]` on the file hash.
   - Call `load_original_file(hash, buf)` to fill the buffer.
   - Parse it with [prc-rs](https://github.com/ultimate-research/prc-rs) (`prc::read_stream`).
   - The same steps work read-only on `ui_chara_db.prc`.
   - [unknown] Whether `load_original_file` returns the version with mods' `.prcxml` patches applied, or only the base or replaced file. ARCropolis's `arcrop_load_file` ([api/file.rs:13](https://github.com/Raytwo/ARCropolis/blob/bf2005b0282a524f242ae8977a70dad23e801318/src/api/file.rs#L13)) loads through its own filesystem, so mod replacements are probably included.
3. **The CSS's own fighter list, the best fit for "any roster layout" [source].** HDR's `init_css_hook` (inline at `0x19eb840`, [HDR-1304 chara_select/layout.rs:49](https://github.com/HDR-Development/HewDraw-Remix/blob/c8f027d502a42eb67a426187921beadff983c3ab/src/chara_select/layout.rs#L49)) runs when the CSS is built:
   - Register `x4` points to a C++ `Vector<u64>` holding the icons that will be shown.
   - Each entry is `0xC1 << 56 | (slot_index & 0xFFFF) << 40 | hash40("ui_chara_X")` (`ui_chara()`, line 196).
   - Masking off the top 24 bits (`KEY_MASK = 0xFFFFFF_0000000000`) gives the hash.
   - The position of `ui_chara_robot` in this vector is its grid position on the roster actually loaded. Only needed if we ever drive the cursor.

**Existing crates or plugins.** [source]

- [prc-rs](https://github.com/ultimate-research/prc-rs) parses and writes `.prc`.
- [arcropolis_api](https://github.com/Raytwo/arcropolis_api) gives `arc_callback` and `load_original_file`. HDR depends on both.
- No crate wraps ui_chara_db at runtime.
- ARCropolis itself hooks the CSS for lazy CSP loading: `css_set_selected_character_ui` and friends (see 2).

**The `CharacterInfo { color_start_index, color_num, original_ui_chara_id }` and "fighter eff data is not one-slotted" log lines [unknown].**

- Not in ARCropolis, HDR, or [smush_extra_slots_effect_fix](https://github.com/ssbucarlos/smush_extra_slots_effect_fix).
- The likely sources are "One Slot Effects [Updated]" ([GameBanana 549058](https://gamebanana.com/mods/549058), which replaced blujay's original, now [marked outdated](https://gamebanana.com/mods/394173)) or Coolsonickirby's [CSK Collection](https://gamebanana.com/mods/499008). `original_ui_chara_id` sounds like CSK's added-character API.
- Neither has public source I could reach: no GitHub repo turned up, and GameBanana downloads are blocked from here.
- Experiment E2 identifies it from the user's SD card.

## 2. CSS runtime state

All offsets in this section come from HDR-1304. **Every CSS offset here is unchanged between 13.0.4 and 13.0.5**: the 13.0.5 port commit `ea1eb702` doesn't touch them. That hints this code region didn't move, which is good for the next update too.

| What | 13.0.4 offset | Notes | Source |
| --- | --- | --- | --- |
| CSS main loop, once per frame while the CSS is up | `0x1a2b570` | Argument is `*const CharaSelect` | [player_port.rs:193](https://github.com/HDR-Development/HewDraw-Remix/blob/c8f027d502a42eb67a426187921beadff983c3ab/src/chara_select/player_port.rs#L193) |
| Player joins a card | `0x1a31020` | `(arg1, port 1..8, arg3, arg4)` | [player_port.rs:346](https://github.com/HDR-Development/HewDraw-Remix/blob/c8f027d502a42eb67a426187921beadff983c3ab/src/chara_select/player_port.rs#L346) |
| Random decided (inline, CSS loop) | `0x1a14280` | `x23` -> `src`. `obj = *(src+8)`. Main chara key at `obj+0x200`, sub at `obj+0x208` | [random.rs:51](https://github.com/HDR-Development/HewDraw-Remix/blob/c8f027d502a42eb67a426187921beadff983c3ab/src/chara_select/random.rs#L51) |
| **Set random fighter data** | `0x1a0d540` | `base[2]` main chara key, `base[3]` sub chara key, byte at `base+0x20` costume, `(*base as u8) - 1` player id | [random.rs:213](https://github.com/HDR-Development/HewDraw-Remix/blob/c8f027d502a42eb67a426187921beadff983c3ab/src/chara_select/random.rs#L213) |
| Copy chara record (inline) | `0x1798ac8` (13.0.5: `0x17989e8`) | HDR copies words [2],[3],[4] from `x0` to `x1` so the forced choice sticks | [random.rs:246](https://github.com/HDR-Development/HewDraw-Remix/blob/c8f027d502a42eb67a426187921beadff983c3ab/src/chara_select/random.rs#L246) |
| Min ready players (inline) | `0x1a2fecc` | `x24+0x16c` game mode, `+0x158` ruleset. Set `w11 = 1` to allow 1 ready player | [mod.rs:276](https://github.com/HDR-Development/HewDraw-Remix/blob/c8f027d502a42eb67a426187921beadff983c3ab/src/chara_select/mod.rs#L276) |
| Card/token teardown helpers | `0x1a1e430`, `0x1a1e660`, `0x1a1e860`, `0x1a1f0b0` | Used to kick a controller off a card | [player_port.rs:420](https://github.com/HDR-Development/HewDraw-Remix/blob/c8f027d502a42eb67a426187921beadff983c3ab/src/chara_select/player_port.rs#L420) |

**Structs [source, partly guessed by HDR themselves].**

- `CharaSelect` ([mod.rs:60](https://github.com/HDR-Development/HewDraw-Remix/blob/c8f027d502a42eb67a426187921beadff983c3ab/src/chara_select/mod.rs#L60)) has these fields:
  - `frames_elapsed`
  - `ready_state` (0 = not ready)
  - `current_player_count`
  - `game_mode`
  - `local_wireless`
  - `max_players_allowed`
  - `player_base`: an array of `PlayerInfo` with a 0x10 stride
- Each `PlayerInfo` points to a `PlayerCard` ([mod.rs:120](https://github.com/HDR-Development/HewDraw-Remix/blob/c8f027d502a42eb67a426187921beadff983c3ab/src/chara_select/mod.rs#L120)), which has:
  - `player_kind`: 0 player, 1 CPU, 2 amiibo, 3 none
  - `controller_id`: 8 means handheld, 0x20
  - `current_id`, a u16 that is probably the hovered or selected slot [inferred]
- HDR's own comment calls these "largely undefined and potentially inaccurate", so read them, don't write them.

**ARCropolis CSS hooks [source], all byte-pattern based:**

- `css_set_selected_character_ui(this, chara_hash_1, chara_hash_2, color, ..)` ([fixes.rs:195](https://github.com/Raytwo/ARCropolis/blob/bf2005b0282a524f242ae8977a70dad23e801318/src/fixes.rs#L195), pattern at [offsets.rs:180](https://github.com/Raytwo/ARCropolis/blob/bf2005b0282a524f242ae8977a70dad23e801318/src/offsets.rs#L180)) fires when a card's selected fighter or color changes. It's the cleanest **observer** for "card shows ui_chara X in color N".
- `change_fighter_color_r/l` (inline, [lib.rs:461](https://github.com/Raytwo/ARCropolis/blob/bf2005b0282a524f242ae8977a70dad23e801318/src/lib.rs#L461)) fire when R/L cycles the color: `w8` is the real color and `w3` the UI color.
- `chara_select_scene_destructor` fires when the CSS is torn down.
- ARCropolis already hooks all of these. A second skyline hook on the same function chains fine.

**What is not in any source I found [unknown]:**

- The cursor (hand) position.
- The hovered panel, apart from `current_id`, maybe.
- **CPU level.** smash has no binding for it, and `PlayerCard` leaves it undefined. Experiment E4 diffs a CPU card at level 1 against level 9.

**Mods that drive the CSS programmatically:**

- HDR's random-select override is the only real one found, and it's the template for SSB-24.
- HDR's port swap kicks and re-adds controllers.
- UTM doesn't touch the CSS.
- Nothing found for "auto select", arcadia-style CSS control, or netplay CSS sync. Arcadia is ARCropolis's own menu framework ([crates/arcadia](https://github.com/Raytwo/ARCropolis/tree/bf2005b0282a524f242ae8977a70dad23e801318/crates/arcadia)). It's useful for scene internals (section 4), not the CSS.

## 3. Costume slot

- **[source]** `FIGHTER_INSTANCE_WORK_ID_INT_COLOR = 0x10000042` ([lua_const.rs:6589](https://github.com/ultimate-research/skyline-smash/blob/d2ca90d9ecffdf9bf58efe6bcf4dd49f62ea1435/src/lua_const.rs#L6589)) is the 0-based slot: "the 8th fox, the index is 7" ([extra slots README](https://github.com/ssbucarlos/smush_extra_slots_effect_fix/blob/8952edb48ee0d30268ecfeb0123304ebca2f7b57/README.md)). `FighterInformation::fighter_color` also exists as a symbol ([cpp.rs:16039](https://github.com/ultimate-research/skyline-smash/blob/d2ca90d9ecffdf9bf58efe6bcf4dd49f62ea1435/src/cpp.rs#L16039)).
- **[source]** The CSS color index is the same number:
  - ARCropolis passes the CSS `color` straight into the function that builds `chara_N_<name>_0C` paths.
  - It wraps next/prev at `color_num`.
  - HDR's costume byte at `base+0x20` takes 0..7 for the same slots.
- **[source, community]** Fandom's [Palette Swap (SSBU)](https://supersmashbros.fandom.com/wiki/Palette_Swap_(Super_Smash_Bros._Ultimate)) lists R.O.B. as: tan, grey, yellow, purple, blue, **green**, cyan, red. Green is 6th, which makes it **index 5 / c05**. SmashWiki asked for a permission prompt that timed out, so it's unchecked.
- **[inferred]** Costume mods that replace a slot keep its index. Added slots (c08+) get new indexes and raise `color_num`. One-slot plugins remap effects, not indexes.
- **Confirm (E3):** pick the green alt and read the `[super-cpu] P2 fighter_kind 45 color N` line the plugin already prints.

## 4. Scene detection

**Scene-push hook [source, two independent sources agree].** The scene manager queues every scene change through one function:

- HDR calls it `scene_transition` (`0x3726120` on 13.0.4, [HDR-1304 lib.rs:382](https://github.com/HDR-Development/HewDraw-Remix/blob/c8f027d502a42eb67a426187921beadff983c3ab/src/lib.rs#L382)).
- Arcadia calls it `scene_queue_push_front` (`0x7103726120` for 13.0.4 and `0x71037266d0` for 13.0.5, [addresses.toml:34](https://github.com/Raytwo/ARCropolis/blob/bf2005b0282a524f242ae8977a70dad23e801318/crates/arcadia/tools/addresses.toml#L34)).

The signature is `(queue, target: *const FixedBaseString64, leaving: *const FixedBaseString64, params)`, and the name is a NUL-terminated string at `target + 8`. The two repos disagree on whether hash or length comes first in the first 8 bytes, so only read the string.

Names seen in source:

- `TitleScene`
- `MainMenuScene`
- `MenuSequenceScene`
- `MeleeRuleScene`
- `CharaSelectScene`
- `StageSelectScene`
- `LocalTopScene`
- `HowToPlayScene`

The in-match and results scene names are **[unknown]**. HDR has a `load_melee_scene` hook at `0x1850810`, which hints at a melee scene. E1 logs them all.

Arcadia's `SceneQueue` also keeps an `active` and a `previous` name ([scene.rs:173](https://github.com/Raytwo/ARCropolis/blob/bf2005b0282a524f242ae8977a70dad23e801318/crates/arcadia/src/game/scene.rs#L173)). That means the current scene could be polled instead of tracked, if we can find the queue pointer [inferred].

**Symbol-based checks [source]**, which survive game updates:

- `FighterManager::is_result_mode` ([cpp.rs:15681](https://github.com/ultimate-research/skyline-smash/blob/d2ca90d9ecffdf9bf58efe6bcf4dd49f62ea1435/src/cpp.rs#L15681)). HDR treats `is_result_mode && entry_count > 0` as "on the results screen" (HDR `fighters/common/src/function_hooks/controls.rs`, `process_inputs_handheld`).
- `FighterManager::is_ready_go`, used by UTM in [common/mod.rs:267](https://github.com/jugeeya/UltimateTrainingModpack/blob/077a8e43b11d1f61ce57169156fb82e92e784c79/src/common/mod.rs#L267).
- `entry_count`.
- `app::smashball::is_training_mode`.
- The plugin already sees `FIGHTER_STATUS_KIND_DEMO` on results.

**Match mode [source].** `get_match_mode(&mut main, &mut sub)` is at `0x1743870` on 13.0.4 ([HDR-1304 utils/src/offsets.rs:95](https://github.com/HDR-Development/HewDraw-Remix/blob/c8f027d502a42eb67a426187921beadff983c3ab/utils/src/offsets.rs#L95)), with a byte pattern next to it. Main mode 0 is Smash and 0xb is Training (`dynamic/src/consts.rs`, `melee_mode`).

**UTM precedent [source].** UTM never detects scenes. It gates everything on `is_training_mode()` and `is_ready_go()`. Its layout draw hook (`OFFSET_DRAW`, a byte pattern, [ui/mod.rs:57](https://github.com/jugeeya/UltimateTrainingModpack/blob/077a8e43b11d1f61ce57169156fb82e92e784c79/src/training/ui/mod.rs#L57)) sees every drawn layout's name (`info_melee`, `info_training`, ...). That's a pattern-based fallback for telling screens apart if the hard offsets break [inferred].

**Skipping the results screen [source].** HDR `game_end` (`0x14d6590`) and `game_exit` (`0x14d7ef0`), both [HDR-1304 lib.rs:245](https://github.com/HDR-Development/HewDraw-Remix/blob/c8f027d502a42eb67a426187921beadff983c3ab/src/lib.rs#L245), call `skip_results`:

- It pushes the states `stateapplyparameters`, `statefadeoutwhenending` and `stateexit` onto the game-state queue, using `0x135a0f0` and `0x135ac80`.
- That leaves the match without showing results.
- It pokes container internals by hand, so it's the riskiest thing in this doc.

## 5. Pressing through menus

- **[source] Matches:** the bot's input path writes `MappedInputs` (button bits after the player's config, per `bridge/bridge/buttons.py`). UTM fills it in the final-input-mapping hook (`OFFSET_FIM`, pattern at [offsets.rs:258](https://github.com/jugeeya/UltimateTrainingModpack/blob/077a8e43b11d1f61ce57169156fb82e92e784c79/src/common/offsets.rs#L258), hook at [training/mod.rs:807](https://github.com/jugeeya/UltimateTrainingModpack/blob/077a8e43b11d1f61ce57169156fb82e92e784c79/src/training/mod.rs#L807)). It overrides CPU control in `OFFSET_SET_CPU_CONTROLS`.
- **[source] Menus:** they read the raw `Controller` struct ([UTM input.rs:240](https://github.com/jugeeya/UltimateTrainingModpack/blob/077a8e43b11d1f61ce57169156fb82e92e784c79/src/common/input.rs#L240): `current_buttons`, `previous_buttons`, `just_down`, `just_release`, sticks, `style`, `npad_number`).
  - UTM opens the vanilla pause menu by setting `current_buttons.plus` and `just_down.plus` on P1's `Controller` ([button_config.rs:300](https://github.com/jugeeya/UltimateTrainingModpack/blob/077a8e43b11d1f61ce57169156fb82e92e784c79/src/common/button_config.rs#L300)).
  - HDR presses A on the results screen by setting `current_buttons.a` and `just_down.a` in a hook on the controller update (`process_inputs_handheld`: `0x36650c0` on 13.0.4, `0x3665670` on 13.0.5, per HDR commits `b6ba2a00` and `ea1eb702`). It presses on about 1 frame in 3 so the game doesn't see a held button.
- **[source]** The UI layer turns controller buttons into its own virtual buttons. Arcadia's `Buttons` puts UP at 0x1, A at 0x20 and B at 0x40, with `VirtualButton::Decide/Cancel` ([layout.rs](https://github.com/Raytwo/ARCropolis/blob/bf2005b0282a524f242ae8977a70dad23e801318/crates/arcadia/src/game/layout.rs)). So `MappedInputs` bits like ATTACK = 0x1 mean nothing to menus.
- **[inferred]** The FIM hook probably doesn't run outside a match, since it works per fighter entry. E8 checks.
- **Conclusion:** menus need a second hook at the `Controller` level, and that hook only helps a port that has a physical or handheld controller attached. A CPU card has no controller, so its CSS state has to be set through the CSS structures, not by pressing buttons.

## Recommended approach

### SSB-24: auto-select green R.O.B.

Don't drive the cursor. Force the decision instead, the way HDR's random override does.

1. **Setup assumption:** the bot plays a CPU card (`player_kind` 1) whose in-match inputs the plugin overrides. That's UTM's `SET_CPU_CONTROLS` precedent, and it fits "beat a level 9 CPU" setups. [unknown] Whether a new CPU card starts on Random (E5).
2. Hook `0x1a0d540` (`set_random_fighter_data`). When `(*base as u8) - 1` is the bot's player id:
   - Set `base[3] = hash40("ui_chara_robot") | (base[3] & KEY_MASK)`.
   - Set the costume byte at `base + 0x20` to 5.
   - Mirror HDR: write before and after `call_original!`.
   - Port HDR's inline copy hook at `0x1798ac8` too, so the choice reaches the match.
3. **Verify in match**, which already works: `fighter_kind == 0x2D` and `color == 5` on the bot's entry. Refuse to log the game otherwise.
4. Optional observer: hook `css_main_loop` (`0x1a2b570`) to report the cards (`player_kind`, `controller_id`, `ready_state`) in a new "menu state" packet.
5. **Fallback** if forcing turns out flaky: have a human pick once. Local Smash keeps picks between games, so steps 3 and 4 alone cover "rematch keeps the same setup" from eval/README item 4.

The roster layout doesn't matter because nothing depends on grid position. `ui_chara_db` only matters to check `color_num(ui_chara_robot) > 5` (section 1, option 1 or 2).

### SSB-25: scene reporting and rematch automation

1. Hook `0x3726120` and keep the latest `target` name in an atomic. Add it, plus `is_result_mode`, to the heartbeat. That's a protocol bump, see `docs/protocol.md`. This alone fixes eval/README items 3 and 6, because the PC can now tell "stuck in a menu" from "between games".
2. Add a controller-level hook (HDR's `0x36650c0`, or find the per-style functions near it) that applies a "menu press" from a new PC packet to a chosen controller's `current_buttons` / `just_down`. Pulse it, don't hold it. The PC decides what to press from the scene:
   - **Results** (`is_result_mode`): A.
   - **CSS** with picks kept: + (Ready to fight). Optionally patch min ready players to 1 at `0x1a2fecc`.
   - **SSS:** A, if the cursor starts on the last stage (unknown, E7). Otherwise pick a ruleset whose stage selection skips the SSS. HDR reads that mode at `text + 0x530996c`, byte 3: modes 3 and 4 skip it ([HDR-1304 lib.rs:305](https://github.com/HDR-Development/HewDraw-Remix/blob/c8f027d502a42eb67a426187921beadff983c3ab/src/lib.rs#L305)).
3. Results skipping (`skip_results`) is a later optimization. Pressing A through results is simpler and lower risk.

## Risks

- **Hardcoded offsets.** Every HDR offset is for one exact build. The plugin already refuses wrong game versions (SSB-10). Keep that, and gate each hook on it. Prefer ARCropolis's byte patterns where they exist: CSS color change, selected-character UI, parameters cache.
- **ARCropolis version.** Current ARCropolis master only boots on 13.0.5, so the user's install is an older 13.0.4 build. Its pattern hooks are probably the same but unverified on that build. Updating the game breaks the HDR-sourced offsets.
- **Undocumented structs.** `CharaSelect` and `PlayerCard` are partly guessed (HDR says so). Read them, keep writes to the two fields HDR writes, and null-check every pointer.
- **Forced fighter vs UI.** Forcing via `set_random_fighter_data` may leave the CSS or results showing Random or the wrong portrait. HDR needed the `0x1798ac8` copy hook for exactly this. That's cosmetic, but check that stock icons and announcer calls don't crash with modded slots.
- **Controller injection** only works on a port that has a `Controller`. A Pro Controller vs handheld vs GC adapter may go through different update functions (HDR only names a "handheld" one).
- **Conflicts.** ARCropolis hooks the same CSS functions. Inline hooks chain, but load order changes register state, so log, don't assume.
- **Licenses.** HDR is AGPL-3.0 and ARCropolis is GPL-3.0. Offsets and struct layouts are facts. Copying their functions verbatim brings license obligations, so write our own.

## Experiments to run on hardware

Each one is a throwaway debug build. Log through `println!`, as the plugin does now.

| # | What to do | What to look for |
| --- | --- | --- |
| E1 | Hook `0x3726120` and print `target` and `leaving` for a full loop: main menu, Smash, CSS, SSS, match, results, back to CSS | Exact scene names for the match and results, and whether a rematch goes results to CSS directly |
| E2 | On the PC: list `sd:/atmosphere/contents/01006A800016E000/romfs/skyline/plugins/` and run `strings <each>.nro \| grep -i "one-slotted\|color_start_index"` | Which plugin prints `CharacterInfo`. Then look for its source or ask its author |
| E3 | Pick each R.O.B. color in turn and play a few seconds | The `color N` log line. Green should be 5 |
| E4 | Hook `0x1a2b570`. Every 60 frames, hex-dump 0x300 bytes of each active `PlayerCard`. Change one CPU's level 1 to 9, then its fighter, then its color | The bytes that track level, fighter, and color. Check `player_kind`, `controller_id` and `current_id` are where HDR says |
| E5 | Add a CPU and don't touch it. Start a match with a hook on `0x1a0d540` that logs `base[0..5]` | Whether a new CPU defaults to Random and whether this function fires for it. Then force `ui_chara_robot` and costume 5, and check the in-match log reads `fighter_kind 45 color 5` |
| E6 | Find ARCropolis's `CSS_SET_SELECTED_CHARACTER_UI` pattern ourselves, hook it, and log `(chara_hash_1, color)` while hovering and cycling colors | Whether the pattern resolves on 13.0.4 and gives the hovered or selected fighter and color per card |
| E7 | Hook `0x36650c0` and log `style`, `npad_number` and `is_valid_controller` per call, with a Pro Controller and a GC adapter. Then pulse A on results and + on the CSS | Whether every controller style passes through, and whether the pulses work. Also where the SSS cursor starts |
| E8 | Count calls of the FIM hook (UTM's `NEEDLE_FIM`) while on the CSS | 0 means a second hook is needed (expected). Above 0 means one hook can do both |
| E9 | Read `cache->databases->character` through ARCropolis's `PARAMETERS_CACHE` pattern, call the color-num function (pattern-find it on 13.0.4) for `0x0e18857219`, or parse `ui_chara_db.prc` through `arc_callback` + prc-rs | `color_num` for R.O.B. (8 vanilla, more with added slots) |
