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
| `bridge/` | Python UDP bridge (`bridge.run`), passive viewer, latency tool, fake Switch, policy interface |
| `training/` | Feature pipeline, action discretization, behavior cloning |
| `eval/` | 50-game eval runner and results log |
| `docs/` | Wire protocol, game hook points (`hooks.md`), logging schema, and `testing-phase0.md` (start here to test on hardware) |

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
python -m bridge.udp_listen                                  # just watch what the Switch sends
python -m bridge.run --policy walk --delay 2 --bot-port 2    # drive the bot
python -m bridge.latency --seconds 60 --bot-port 2           # measure round trip delay
```

Only one of them can run at a time (they all own UDP 4243). `--bot-port` has to match `bot_port` in the plugin's config. To test the PC side without a Switch, run `python -m bridge.fake_switch` in a second terminal; it takes inputs and sends acks like the plugin.

Phase 1 tools (from `bridge/`):

| Command | What it does |
| --- | --- |
| `python -m bridge.udp_listen --log --stage fd` | also writes each match to `data/matches/` (format in `docs/logging-schema.md`) |
| `python -m bridge.inspect_log data/matches/*.npz` | summary of logged matches |
| `python -m bridge.run --policy scripted --log --eval 50` | plays, logs every match (with both ports' inputs) and scores 50 games into `eval/results.csv`, one process |
| `python -m bridge.eval --games 50 --model scripted` | scores N matches without playing (human or CPU sets, see `eval/README.md`) |
| `python -m bridge.fake_switch --matches 3` | fake Switch that plays 3 short matches with a winner each, takes inputs, sends captured inputs |

The scripted R.O.B. baseline (SSB-11) is `bridge/bridge/scripted.py`, selectable as `--policy scripted`. It picks the stage layout from the streamed stage id. `SCPU_STAGE=battlefield|fd|omega` is only a fallback for other stages. Under `bridge.run`, `frame.bot` is already the `--bot-port` fighter, so `SCPU_BOT_PORT` isn't needed there.

Human play for imitation learning (SSB-13): the plugin captures both ports' final mapped inputs every frame (protocol v3), so `python -m bridge.udp_listen --log --p1 NAME --p2 NAME` logs a human set with the input columns filled.

Pin the game version and mod set in `plugin/src/version.rs` before collecting any data.
