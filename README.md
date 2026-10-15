# super-cpu

A Smash Bros. Ultimate CPU opponent that plays far above a level 9 CPU, running on a modded Switch. A Skyline plugin streams game state to a PC and applies the inputs a trained policy sends back.

Local play only. Never point this at online matchmaking.

## v1 target

One character, mirror match, one stage, 3 stock, no items. Win 90% or more of a 50-game set against a level 9 CPU on real hardware.

## How it fits together

```
Switch (Skyline plugin)  --state, every frame-->  PC bridge  -->  policy
         ^                                           |
         +-------------- inputs <--------------------+
                                                     |
                                              logger -> dataset -> training
```

The emulator, when used, runs the same plugin and talks to the same bridge.

## Layout

| Path | What lives there |
| --- | --- |
| `plugin/` | Rust Skyline plugin: reads fighter state, applies inputs, version guard |
| `bridge/` | Python TCP server, message protocol, policy interface |
| `training/` | Feature pipeline, action discretization, behavior cloning |
| `eval/` | 50-game eval runner and results log |
| `docs/` | Wire protocol, logging schema, and `testing-phase0.md` (start here to test on hardware) |

## Phases

| Phase | Focus | Gate |
| --- | --- | --- |
| 0 | Plumbing | PC commands move the bot, delay is measured |
| 1 | Logging and baseline | A 50-game eval runs unattended and logs results |
| 2 | Behavior cloning | Cloned bot beats level 9 in over half its games |
| 3 | DAgger and emulator parity | Hardware win rate within 10 points of emulator |
| 4 | RL fine-tune and human testing | v1 target met |

Stories are tracked as SSB-1 through SSB-22. Reference the story key in commits and PRs, for example `SSB-3: read damage and stocks`.

## Getting started

Plugin (needs [cargo-skyline](https://github.com/jam1garner/cargo-skyline)):

```
cd plugin
cargo skyline build --release
```

Bridge (Python 3.11+):

```
cd bridge
python -m venv .venv && source .venv/bin/activate
pip install -e .
python -m bridge.udp_listen
```

To test the PC side without a Switch, run `python -m bridge.fake_switch` in a second terminal.

Pin the game version and mod set in `plugin/src/version.rs` before collecting any data.
