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

## 7. Inputs: the PC moves the bot (SSB-5, SSB-6, SSB-7)

Background on the hooks and the open questions these checks answer: `docs/hooks.md`.

### 7a. PC side first, no Switch

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

### 7b. Setup on the Switch

1. Rebuild the plugin and copy the new `.nro` over.
2. Add `bot_port = 2` to `config.toml` (a fresh template already has it). The bridge's `--bot-port` has to match; it warns if it doesn't.
3. The bot's port needs a controller. Plug in a second controller or use a single Joy-Con, and pick the bot's character with it. A CPU slot won't work yet (see hooks.md for why and how we'll find out).
4. Allow the replies through Windows Firewall: the rule from step 4 already covers inbound UDP 4243. Inputs go out from the PC, so nothing else is needed.
5. Stop `udp_listen` first. Only one program can own port 4243.

### 7c. Checks

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

### 7d. Latency

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
| `bridge.run` says "input hook NOT installed" | The final input mapping pattern wasn't found (game version?). State still works. Send me the `cargo skyline listen` boot log |
| "receiving NO" | The Switch socket couldn't be made non-blocking, so inputs are off to protect the state stream. Send me the boot log |
| Inputs sent, status line `switch: rx 0 applied 0`, `rejected` climbing | Packets arrive but get refused. Usually the PC's IP isn't the `pc_ip` in config.toml (the plugin only takes input from that IP, e.g. a PC with two network adapters) |
| Inputs sent, `rx` and `rejected` both stay 0 | Replies aren't reaching the Switch. Router or AP isolation dropping them, or a firewall on the PC blocking outbound UDP |
| `late` climbing as fast as `applied` | The network is slower than `--delay`. Raise it, or run `bridge.latency` to pick one |
| `applied` climbing but the bot doesn't move | Wrong port: check the "plugin drives port" warning, then the port-or-slot check in 7c |
| The bot moves but so does P1 | `player_idx` is the controller slot, see 7c |
| "plugin drives port X but --bot-port is Y" | Make `--bot-port` match `bot_port` in config.toml |

## Seeing plugin logs

`cargo skyline listen` (with your Switch's IP set via `cargo skyline set-ip`) streams the plugin's `println!` output while the game runs.
