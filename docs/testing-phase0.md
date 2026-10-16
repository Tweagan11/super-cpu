# Testing Phase 0

Work through these in order. Each step only adds one new thing that can break.

## 1. PC side, no Switch

From `bridge/`:

```
python -m unittest discover -s tests -t .
```

Then in two terminals:

```
python -m bridge.udp_listen
python -m bridge.fake_switch --drop 0.05
```

The listener should report "Switch found" and a missing rate near 5%.

## 2. Build the plugin

From `plugin/` (needs Rust nightly and `cargo install cargo-skyline`):

```
cargo skyline build --release
```

The output is `target/aarch64-skyline-switch/release/libsuper_cpu.nro`.

## 3. Install on the Switch

1. Find your PC's local IP (`ipconfig` on Windows, the IPv4 address on your Wi-Fi or Ethernet adapter).
2. Copy `libsuper_cpu.nro` to `sd:/atmosphere/contents/01006A800016E000/romfs/skyline/plugins/`.
3. Boot Ultimate once. The plugin writes `sd:/ultimate/super-cpu/config.toml`. Close the game.
4. Edit that file so `pc_ip` is your PC's IP. Put the SD back.

Or create the config file by hand before the first boot:

```
pc_ip = "192.168.1.50"
udp_port = 4243
bot_port = 2
```

## 4. Allow the traffic on Windows

Windows Firewall blocks inbound UDP by default. Run once in an admin PowerShell:

```
New-NetFirewallRule -DisplayName "super-cpu UDP" -Direction Inbound -Protocol UDP -LocalPort 4243 -Action Allow -Profile Private
```

Your home network has to be set to Private for that rule to apply.

## 5. First real test

1. Start `python -m bridge.udp_listen` on the PC.
2. Boot Ultimate on the Switch, same network as the PC.
3. Within a few seconds of the title screen, the listener should print "Switch found at ..." and then a status line every 5 seconds.

Pass: heartbeats arrive, game version reads 13.0.4, the game plays normally.

## 6. Fighter state (SSB-3 first cut)

1. Rebuild the plugin and copy the new `.nro` over the old one.
2. Start `python -m bridge.udp_listen`, then start a match. Training mode is easiest.
3. The listener prints "match started", then both fighters once a second: name, damage, position, stocks, ground or air.

Checks:

| Do this | Expect on the PC |
| --- | --- |
| Hit the CPU a few times | Its damage matches the number on screen |
| Jump | `air`, and pos_y goes up |
| Walk left and right | pos_x goes down and up |
| Lose a stock (or let the CPU fall off) | Stock count drops |
| Watch the 5-second status line | Frames arriving at close to 60/s, skipped near 0 |

The fake Switch now plays a fake match too: `python -m bridge.fake_switch` shows what the output should look like.

## 7. Remaining fields and the version guard (SSB-3 / SSB-10)

1. Rebuild, recopy, start `python -m bridge.udp_listen`.
2. If you have `cargo skyline listen` running, the boot log should say `game version 13.0.4 matches, state streaming on`.
3. Start a training mode match as R.O.B. vs R.O.B. on Battlefield.

Each fighter line now looks like this:

```
P1 robot   12.0%  (  -30.1,   0.0) v(+0.00, +0.00)  3 stk  sh 50.0  j2  ground WAIT
```

`v(x, y)` is velocity per frame, `sh` is shield health, `j` is jumps left, and `HITSTUN` shows up before the status name while it's on.

| Do this | Expect on the PC |
| --- | --- |
| Look at the line right after "match started" | `stage BattleField (id 0, layout battlefield)` |
| Stand still | `v(+0.00, +0.00)` |
| Run right, then left | vel x positive, then negative |
| Jump | vel y positive going up, negative coming down |
| Stand on the ground | `j2` (R.O.B. has one ground jump + one double jump) |
| Jump once | `j1` |
| Double jump | `j0`, back to `j2` after landing |
| Hold shield a few seconds | `sh` goes down from 50; it climbs back after you let go |
| Let the CPU hit you with something strong | `HITSTUN` while you're in the hurt animation (not during tumble, that's on purpose) |
| Check the plugin log (`cargo skyline listen`) at match start | one line per fighter, like `P1 fighter_kind 45 color 3 (stage_id 0)`. **Tell me which color number green R.O.B. is.** |

Stage ids, one match each (just check the stage line):

| Stage | Expect |
| --- | --- |
| Battlefield | `BattleField (id 0, layout battlefield)` |
| Final Destination | `End (id 3, layout fd)` |
| Battlefield, Omega form | `End_BattleField (id 1, layout fd)` |
| Any other Omega stage | `End_<something>`, `layout fd` |

If any of those ids come out different, send me the line. Stage ids come straight from the game, but the name table is from the community bindings.

Version guard (only if you want to see it trip): set `EXPECTED_GAME_VERSION` in `plugin/src/version.rs` to something wrong like `"13.0.3"`, rebuild, and start a match. Expect: heartbeats keep arriving, the listener's 5-second status line keeps going, no "match started" ever, and the boot log says `GAME VERSION MISMATCH`. Put it back to `"13.0.4"` afterwards. The listener's warning only fires when the *Switch* is on a different version than `--expect-version`, so you can also see it with `python -m bridge.udp_listen --expect-version 13.0.3`. You can try all of this without a Switch: `python -m bridge.fake_switch --version 13.0.3`.

## 8. Measure the stages (for the scripted bot)

The scripted bot's ledge and platform positions in `bridge/bridge/stages.py` are guesses. Measure the real ones once per stage:

1. Run `python -m bridge.measure` instead of the listener (it shows the same output plus a measurement).
2. Start a training match on Battlefield.
3. Walk R.O.B. right up to the left edge until he teeters, then the right edge. Stand on each of the three platforms for a second, walking to both ends of each.
4. Leave the match (or press Ctrl+C). It prints the main stage edges, ground height, platforms, and a `StageLayout(...)` block.
5. Repeat on Final Destination (just the two edges).

Send me both printouts and I'll put the numbers in `stages.py`.

## 9. Inputs: the PC moves the bot (SSB-5, SSB-6, SSB-7)

Background on the hooks and the open questions these checks answer: `docs/hooks.md`.

### 9a. PC side first, no Switch

```
python -m bridge.run --policy walk --delay 2 --bot-port 2
python -m bridge.fake_switch
```

Expect "Switch found", "plugin: bot port 2 ... input hook installed, receiving yes", and a status line every 5 s with `applied` climbing, `late 0`, `rt med 2`. Then try the latency tool against a fake 20 ms network:

```
python -m bridge.latency --seconds 10
python -m bridge.fake_switch --latency-ms 20
```

Round trip median should be about 3 to 4 frames.

### 9b. Setup on the Switch

1. Rebuild the plugin and copy the new `.nro` over.
2. Add `bot_port = 2` to `config.toml` (a fresh template already has it). The bridge's `--bot-port` has to match; it warns if it doesn't.
3. The bot's port needs a controller. Plug in a second controller or use a single Joy-Con, and pick the bot's character with it. A CPU slot won't work yet (see hooks.md for why and how we'll find out).
4. Allow the replies through Windows Firewall: the rule from step 4 already covers inbound UDP 4243. Inputs go out from the PC, so nothing else is needed.
5. Stop `udp_listen` first. Only one program can own port 4243.

### 9c. Checks

Start a versus match (not training mode) with you on port 1 and the bot's controller on port 2, then:

```
python -m bridge.run --policy walk --delay 2 --bot-port 2
```

| Do this | Expect |
| --- | --- |
| Start the bridge mid-match | Within a second the P2 fighter walks toward you and stops close by. Its own controller does nothing |
| Walk around with your controller | P1 moves normally, no lag, no stuck inputs. The bot follows you |
| Ctrl+C the bridge | Within 2 s the P2 controller works again |
| Start it again | The bot takes over again |
| Go to the menus with the bridge running | Menus work normally on both controllers (the plugin only drives during a match) |
| Run a whole match without the bridge | Plays exactly like before this change |
| Watch the status line | `late 0` or near it, `neutral` only rising when you stop the bridge, `rejected 0` |

Write down what the bridge prints in its `hooks:` line (every 30 s): `input player_idx seen [...]` and `cpu controller_index seen [...]`.

**Port or controller slot?** Quit to the character select, and this time pick P1's character with the controller that was the bot's and P2's with yours (so the controller order is the reverse of the port order). Start the bridge. If P2 still walks toward P1, `player_idx` is the port and you're done. If P1 walks instead, it's the controller slot: tell me, and for now set `fim_player_idx` in `config.toml` to the slot the bot's controller is in (0-based).

**Can it drive a CPU?** Set up P1 human, P2 a CPU (any level), set `bot_port = 2`, start the bridge. If the CPU walks toward you, the final input mapping covers CPUs too and we can drop the extra controller. If it plays like a normal CPU, it doesn't. Either answer is useful; send the `hooks:` line.

### 9d. Latency

With a match running and the bot's controller on port 2:

```
python -m bridge.latency --seconds 60 --bot-port 2
```

Run it twice: Switch on Wi-Fi, then wired (USB Ethernet adapter in the dock). Write both results down here:

| Network | Round trip median | p99 | max | late | lost |
| --- | --- | --- | --- | --- | --- |
| Wi-Fi | | | | | |
| Wired | | | | | |

The p99 round trip is the training delay (training/README.md: "train with input delay randomized from 0 to the worst case measured in SSB-7"), and it's the smallest `--delay` that won't drop inputs as late. Expect 2 frames at best (state for frame N leaves when N + 1 starts, so N + 1 is already gone), 3 to 4 on a good network.

## 10. Input capture: human inputs in the stream (SSB-13)

Plugin and bridge are now protocol v4, so rebuild and recopy the `.nro` first (an old plugin shows up as "protocol v3, expected v4").

### 10a. PC side first

```
python -m bridge.udp_listen
python -m bridge.fake_switch
```

Each fighter line now ends with the captured input, like `in L(+100,  +0) C(  +0,  +0) ATTACK`. The fake's sticks sweep and press jab every 40 frames. Then the whole thing in one process:

```
python -m bridge.run --policy scripted --log --eval 3 --model scripted --min-seconds 2 --results /tmp/r.csv
python -m bridge.fake_switch --matches 3 --match-seconds 4
```

Expect 3 `[log] wrote` lines, 3 `[eval] game` lines and `done: 2/3 wins`. `python -m bridge.inspect_log data/matches/*.npz` says `inputs: present`.

### 10b. On the Switch

Versus match, you on port 1, a second controller on port 2, **no bridge running**, `python -m bridge.udp_listen`:

| Do this | Expect |
| --- | --- |
| Hold right on P1's stick | P1's line shows `L(+1xx,  +0)`, P2's stays near 0 |
| Hold right on P2's controller | P2's line shows it, P1's doesn't |
| Press A, B, X, shield on P1 | `ATTACK`, `SPECIAL`, `JUMP`, `GUARD ...` on the P1 line only (names depend on your button config, that's the point: these are mapped inputs) |
| Flick the c-stick | `C(...)` moves, `CSTICK_ON` shows |
| Stand still | `L(  +0,  +0) C(  +0,  +0) -`, no stray buttons |
| Pause | lines stop (no state while paused), no crash |
| Watch the 5 s status line | `captured as P1/P2 player_idx [0, 1]` |

**Port or controller slot, again.** Swap which controller picks which port at character select (like 9c). If P1's line still follows the controller on port 1, `player_idx` is the port. If the lines follow the controllers instead, it's the slot: tell me, set `fim_player_idx` (that swaps both captured ports), and note that logs made before have the input columns swapped (their header's `input_capture.mapping` says which mapping they used).

Then with the bridge driving P2 (`python -m bridge.run --policy walk --log`): finish a match and run `inspect_log` on it. Both ports should be `inputs_present`, and the `.json` header's `input_capture.ack_capture_mismatches` should be 0. Nonzero means the captured frame and the applied frame disagree: send me the header.

### 10e. Control module inputs: CPU vs CPU (v4)

This is what lets level 9 R.O.B. vs R.O.B. matches log inputs with nobody holding a controller.

PC side first: `python -m bridge.udp_listen` and `python -m bridge.fake_switch --cpu-ports 1,2` to see packets arrive, and `test_capture.py` for the logger side.

On the Switch:

1. Start the game with the new `.nro`. In the plugin log (`udp_listen` shows it, or the Skyline log) look for `control module: pad button table matches the game constants`. Any `pad button X is ...` line means the button translation is off: send it to me before trusting anything else.
2. Versus match, both ports set to CPU level 9, R.O.B. vs R.O.B., no bridge needed (`python -m bridge.run --policy walk --log` with no session is fine too, or just `udp_listen` + the logger of your choice). Let one stock go by, then finish the match.
3. `python -m bridge.inspect_log data/matches/<newest>.npz`. Expect `inputs: present`, and under each port `input sources: control_module` with `rows_from_control_module` roughly the frame count. Look at the `.json`: `input_capture.port_sources` is `["control_module", "control_module"]` and `rows_control_module` close to `frame_count` on both ports.
4. Sanity check the data: P1's `stick_x` column should not be all zeros, `buttons` should show `ATTACK`/`SPECIAL`/`JUMP` bits at times (R.O.B. CPUs use all three), and `input_valid` is 1 on both ports.
5. Human agreement (validates the translation): one match with you on port 1, human controller, any opponent. Press every button a few times, move the stick through its range, flick the c-stick, shield, grab. Finish it and look at `input_capture.control_module_agreement[0]`: expect `rows_compared` close to `frame_count` and `all_pct` above 95. If lower, `buttons` below `rows_compared` means the button table or timing is off (try the shield and grab presses first), `main` or `cstick` means the stick scaling or axes. Send me the header.

Known quirks to expect, not bugs: frame 0 of a match; a few rows in hitlag where the CPU holds its last input; `GUARD_HOLD`/`*_RAW` bits may differ between sources and are ignored by the agreement check.

## 11. Scenes and unattended rematches (SSB-25)

Adds the scene packet (kind 7), menu presses (kind 8) and `bridge.run --auto-rematch`. Rebuild and recopy the `.nro`. No existing packet changed.

### 11a. PC side first, no Switch

```
python -m bridge.run --policy walk --auto-rematch --idle-timeout 30
python -m bridge.fake_switch --matches 3 --match-seconds 4 --menus
```

Expect `[run] scene: 'MeleeScene'`, then after each match `scene: 'ResultScene'`, an `[rematch] A on 'ResultScene', press 1/12` line, `scene: 'CharaSelectScene'`, `[rematch] + on ...`, and the next match. Add `--menu-ignore 1` to the fake and watch it retry with a longer wait. `python -m bridge.udp_listen` prints `[listen] scene: ...` lines instead.

### 11b. E1: scene names on the Switch (no presses yet)

`auto_menu` stays off (the default). Start `python -m bridge.udp_listen`, boot the game, and walk a full loop by hand: main menu, Smash, CSS, stage select, play a match to the end, results, back to the CSS.

| Where | Expect |
| --- | --- |
| Boot | `[switch] scene hook installed at text+0x3726120`, `auto_menu off` |
| Every screen change | `[switch] scene #n: "A" -> "B" (leaving arg "...")` and `[listen] scene: 'B' (change #n, menu hook off, ...)` |

Write down the exact names of the CSS, match and results scenes. If they don't contain the defaults in `bridge/bridge/scenes.py` (`CharaSelect`, `Melee`, `Result`), fix them there or pass `--css-scene`, `--match-scene`, `--results-scene` to `bridge.run`; the plugin's own match check (`scene.rs`, "melee" and not "result") needs the same fix. Also note whether a rematch goes results to CSS directly, and whether a stage select scene sits in between (that screen needs its own press, which this does not do: skip it with the ruleset or make the cursor start on the right stage, see `css-research.md`). No scene lines at all means the offset or the hook is wrong for this build: send the boot log.

### 11c. Menu presses (auto_menu = true)

Set `auto_menu = true` in `config.toml` and boot. Expect `auto_menu on: controller hook at text+0x36650c0, bot npad N` and `controller update: style S npad N id I` lines: write down which (style, npad) combos show up and whether the bot port's controller is one of them. If the game crashes on boot or at the first menu, set `auto_menu` back to false and send the crash report (the hook prototype is the top risk, `hooks.md` section 5).

Then run `python -m bridge.run --policy walk --auto-rematch` and finish a match. The `[rematch]` lines say what it pressed; `[run] scene:` shows `menu pulses applied` climbing.

| Do this | Expect |
| --- | --- |
| Finish a match with the bridge running | The results screen goes on to the CSS with no controller touched |
| On the CSS | `+` starts the next match, no more presses once it did |
| A press that doesn't land | Retries after 1.6 s, 2.6 s, 4.1 s, up to 12 presses, then `giving up` |
| During a match | No presses, `menu pulses applied` stays flat |
| The other port's controller | Doesn't react |

If A does nothing on the results screen, set `PRESS_HOLD_FRAMES` to 4 in `bridge/bridge/rematch.py` (two pulses). If + doesn't start the match, the CSS probably needs its picks set first (README item 4).

### 11d. Unattended 3-match run

With the names fixed, the cards set up, and the stage select skipped or settled:

```
python -m bridge.run --policy scripted --log --eval 3 --model scripted --auto-rematch --min-seconds 5
```

Start the first match by hand and walk away. Expect 3 `[log] wrote` lines and 3 `[eval] game` lines, with only a handful of `[rematch]` lines per screen. Anything stuck for over a minute is a wrong scene name or an unpressed screen: look at the last `[run] scene:` line.

## 12. Forced green R.O.B. (SSB-24)

The plugin forces the fighter and costume at the decided-fighter record, so nobody has to move a cursor. This is experiment E5 from `css-research.md`, with the forcing on. Game 13.0.4 only.

### 12a. Config

Add to `sd:/ultimate/super-cpu/config.toml`:

```
force_fighter = true
force_ports = [2]
force_costume = 5
```

Boot the game and watch `bridge.run` or `udp_listen`. Expect `[switch] force_fighter on: ports [2] -> robot c05, hook at text+0x1a0d540, copy hook at text+0x1798ac8`. A crash on boot means the inline copy hook: set `force_copy_hook = false` and try again, and tell me.

### 12b. Random on the bot's card

Put the bot's card (port 2) on Random, start the match, and keep the log. Expect, in order:

- `force pre: P2 saw hash 0x... c.. raw w0 0x2 ...; wrote hash 0x0e18857219 c05`, then `force post: ...`, and a `dump ... before:` line. Keep these lines, they are the E5 answer (does it fire, what the record looks like).
- `force check: P2 robot c05 OK` right after `match started`, and green R.O.B. in the match.

If P2 is the wrong fighter or `MISMATCH`: look at the dump. If the old hash is in the second 8 byte group (`w2`), the main key is word 2, not 3.

### 12c. Non-Random pick

Rematch with the bot's card on a fixed character (say Mario). Does `force pre` still show up? If it does and the check says OK, the forcing works for any pick. If there are no `force` lines, the function only runs for Random and the CPU card needs another route; report it.

### 12d. A CPU card

Use a CPU card on port 2 (no controller assigned, level 9) and leave it untouched. Same expectations. Then, as a second test, `force_ports = [1, 2]` with a human on port 1: P1 should become R.O.B. as well, and the check line for P1 appears too (the in-match check covers ports 1 and 2).

### 12e. Off by default

Remove the `force_` lines (or set `force_fighter = false`), reboot: no `force` lines, nothing hooked.

## If nothing shows up

| Symptom | Likely cause |
| --- | --- |
| Game boots, listener says "nothing yet" | Wrong `pc_ip`, firewall rule missing, or Switch and PC on different networks (guest Wi-Fi, AP isolation) |
| No `config.toml` was created | Plugin didn't load. Check the `.nro` path and that Skyline/ARCropolis load at all |
| Game crashes on boot | Remove the `.nro` and tell me the crash report from `sd:/atmosphere/crash_reports/` |
| Missing rate above a few percent on Wi-Fi | Expected on busy Wi-Fi; try a USB Ethernet adapter in the dock |
| Listener says "protocol v1, expected v2" (or "v2, expected v3") | The Switch still has the old plugin. Rebuild and recopy the `.nro` |
| Fighter lines show `in --` on a v3 plugin | Inputs not captured: input hook not installed, or `live_frame()` keeps losing its try_lock (see hooks.md, FIM thread question) |
| Inputs show on the wrong port's line | `player_idx` is the controller slot, see 10b |
| "hook NOT installed" | The game's common module never triggered the hook install. Check that `libnro_hook.nro` is in the plugins folder |
| Hook installed, fighter frames stays 0 during a match | The hooked function isn't running for fighters. Send me the listener output |
| Fighter frames climbing, frames queued stays 0 | The hook runs but the state reader drops every frame. Send me the listener output |
| Game crashes when a match starts | Remove the `.nro` and send me the crash report from `sd:/atmosphere/crash_reports/` |
| Stocks show `?` | The FighterManager lookup failed; everything else still works |
| Heartbeats arrive but never "match started", and the listener warns about the game version | The version guard tripped: the Switch isn't on 13.0.4. Expected behavior, no state is sent on the wrong build |
| Heartbeat game version shows empty or garbage | The version read at boot failed. Send me the listener output |
| Game crashes when a match starts on the new build only | One of the new reads (velocity, shield, jumps, stage) is bad. Send the crash report |
| `bridge.run` says "input hook NOT installed" | The final input mapping pattern wasn't found (game version?). State still works. Send me the `cargo skyline listen` boot log |
| "receiving NO" | The plugin couldn't start receiving. It now says why in a `[switch] inputs off: ...` line in `udp_listen`/`bridge.run` (plugin messages show up there now, no `cargo skyline listen` needed). Send me that line |
| Inputs sent, status line `switch: rx 0 applied 0`, `rejected` climbing | Packets arrive but get refused. Usually the PC's IP isn't the `pc_ip` in config.toml (the plugin only takes input from that IP, e.g. a PC with two network adapters) |
| Inputs sent, `rx` and `rejected` both stay 0 | Replies aren't reaching the Switch. Router or AP isolation dropping them, or a firewall on the PC blocking outbound UDP |
| `late` climbing as fast as `applied` | The network is slower than `--delay`. Raise it, or run `bridge.latency` to pick one |
| `applied` climbing but the bot doesn't move | Wrong port: check the "plugin drives port" warning, then the port-or-slot check in 9c |
| The bot moves but so does P1 | `player_idx` is the controller slot, see 9c |
| `force check: P2 NOT FORCED` or no `force` lines with `force_fighter = true` | The decided-fighter hook doesn't run for that card (see 12c/12d), or the first argument isn't the record. Send me the log |
| `force check: P2 MISMATCH` | Hash in the wrong word or the copy hook not carrying it. Send me the `dump` lines (12b) |
| "plugin drives port X but --bot-port is Y" | Make `--bot-port` match `bot_port` in config.toml |

## Seeing plugin logs

`cargo skyline listen` (with your Switch's IP set via `cargo skyline set-ip`) streams the plugin's `println!` output while the game runs.
