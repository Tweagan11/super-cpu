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

## If nothing shows up

| Symptom | Likely cause |
| --- | --- |
| Game boots, listener says "nothing yet" | Wrong `pc_ip`, firewall rule missing, or Switch and PC on different networks (guest Wi-Fi, AP isolation) |
| No `config.toml` was created | Plugin didn't load. Check the `.nro` path and that Skyline/ARCropolis load at all |
| Game crashes on boot | Remove the `.nro` and tell me the crash report from `sd:/atmosphere/crash_reports/` |
| Missing rate above a few percent on Wi-Fi | Expected on busy Wi-Fi; try a USB Ethernet adapter in the dock |
| Heartbeats arrive but no "match started" | The frame hook isn't running. If Training Modpack is installed, it patches the same slot; try removing it temporarily |
| Game crashes when a match starts | The vtable offset in `plugin/src/frame.rs` is wrong for 13.0.4. Remove the `.nro` and send me the crash report |
| Stocks show `?` | The FighterManager lookup failed; everything else still works |

## Seeing plugin logs

`cargo skyline listen` (with your Switch's IP set via `cargo skyline set-ip`) streams the plugin's `println!` output while the game runs.
