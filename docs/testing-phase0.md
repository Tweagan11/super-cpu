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

## If nothing shows up

| Symptom | Likely cause |
| --- | --- |
| Game boots, listener says "nothing yet" | Wrong `pc_ip`, firewall rule missing, or Switch and PC on different networks (guest Wi-Fi, AP isolation) |
| No `config.toml` was created | Plugin didn't load. Check the `.nro` path and that Skyline/ARCropolis load at all |
| Game crashes on boot | Remove the `.nro` and tell me the crash report from `sd:/atmosphere/crash_reports/` |
| Missing rate above a few percent on Wi-Fi | Expected on busy Wi-Fi; try a USB Ethernet adapter in the dock |

## Seeing plugin logs

`cargo skyline listen` (with your Switch's IP set via `cargo skyline set-ip`) streams the plugin's `println!` output while the game runs.
