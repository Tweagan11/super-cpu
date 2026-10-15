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

## If nothing shows up

| Symptom | Likely cause |
| --- | --- |
| Game boots, listener says "nothing yet" | Wrong `pc_ip`, firewall rule missing, or Switch and PC on different networks (guest Wi-Fi, AP isolation) |
| No `config.toml` was created | Plugin didn't load. Check the `.nro` path and that Skyline/ARCropolis load at all |
| Game crashes on boot | Remove the `.nro` and tell me the crash report from `sd:/atmosphere/crash_reports/` |
| Missing rate above a few percent on Wi-Fi | Expected on busy Wi-Fi; try a USB Ethernet adapter in the dock |
| Listener says "protocol v1, expected v2" | The Switch still has the old plugin. Rebuild and recopy the `.nro` |
| "hook NOT installed" | The game's common module never triggered the hook install. Check that `libnro_hook.nro` is in the plugins folder |
| Hook installed, fighter frames stays 0 during a match | The hooked function isn't running for fighters. Send me the listener output |
| Fighter frames climbing, frames queued stays 0 | The hook runs but the state reader drops every frame. Send me the listener output |
| Game crashes when a match starts | Remove the `.nro` and send me the crash report from `sd:/atmosphere/crash_reports/` |
| Stocks show `?` | The FighterManager lookup failed; everything else still works |
| Heartbeats arrive but never "match started", and the listener warns about the game version | The version guard tripped: the Switch isn't on 13.0.4. Expected behavior, no state is sent on the wrong build |
| Heartbeat game version shows empty or garbage | The version read at boot failed. Send me the listener output |
| Game crashes when a match starts on the new build only | One of the new reads (velocity, shield, jumps, stage) is bad. Send the crash report |

## Seeing plugin logs

`cargo skyline listen` (with your Switch's IP set via `cargo skyline set-ip`) streams the plugin's `println!` output while the game runs.
