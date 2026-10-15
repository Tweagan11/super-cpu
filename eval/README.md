# eval

The 50-game eval (SSB-12) is the scoreboard for every model change.

Each run records, per model version: date, game version, mod hash, platform (Switch or emulator), stage layout, opponent (level 9 CPU or a named player), games played, wins, stocks taken, and average damage dealt.

Results go in `results.csv` so they're easy to compare and chart. Compare Switch and emulator runs of the same model for the parity check (SSB-20).

## Running it

The harness is `bridge/bridge/eval.py`, run as `python -m bridge.eval` from `bridge/`. It lives in the bridge package rather than here because it reuses the bridge's match detection and logger, and `bridge` is the only installed package. This folder holds the results and these docs.

```
cd bridge
python -m bridge.eval --games 50 --model scripted --opponent cpu9 --bot-port 2 --platform switch --stage fd
```

| Flag | Default | Notes |
| --- | --- | --- |
| `--games` | 50 | games to count |
| `--model` | required | name of the bot under test, goes in the row |
| `--opponent` | cpu9 | |
| `--bot-port` | 2 | which port is the bot (1 or 2) |
| `--platform` | switch | `switch` or `emulator` |
| `--stage` | fd | fallback layout for stages that aren't Battlefield or FD/Omega. The real layout comes from the streamed stage id |
| `--expect-version` | 13.0.4 | matches from another game version don't count |
| `--mod-hash` | unset | |
| `--min-seconds` | 10 | shorter matches don't count (someone backing out of a match) |
| `--no-log` | off | skip the per-frame match logs |
| `--idle-timeout` | none | stop if nothing arrives for this long |

Run one stage per run so rows split cleanly by stage. If a run does mix stages (stage_id streamed and someone changes stage), the row's stage reads `mixed` and the per-match CSV has the real one.

## What it writes

- `eval/results.csv`: **one row per run**, totals over all N games. Only written once all N games are in, so a half-finished run never shows up as a result.
- `data/eval/<run_id>.csv`: one row per match, appended as each match ends (bot and opponent fighter, frames, end reason, winner, win, stocks taken/lost, damage dealt/taken, log path). Crash-safe: an interrupted run keeps everything it saw.
- `data/matches/<match_id>.npz/.json`: the per-frame log for each match (see `docs/logging-schema.md`).

Scoring: a win is "bot has more stocks than the opponent on the last frame", falling back to who's in DEAD on the last frame when stocks tie. Stocks taken is the opponent's stock drops. Damage dealt is the sum of the opponent's damage increases, so it counts across stocks. `avg_damage_dealt` is per game.

A match doesn't count if its game version is wrong, it's shorter than `--min-seconds`, or it was cut off. The harness prints why and keeps waiting.

The `stage` column was added to `results.csv` for SSB-8. The harness upgrades an older header in place (old rows get an empty stage).

## Running it alongside the bot

`python -m bridge.eval` only watches the state stream. It doesn't send inputs. The bot itself runs through `bridge.run --policy NAME` (being built on another branch). Both bind UDP 4243, so they can't both run as separate processes on the same port. Two ways to deal with that:

1. Have `bridge.run` pass `EvalSession(...).observers()` to its `MatchWatcher`, so one process plays and scores. This is the intended setup once `bridge.run` lands, and it's a few lines.
2. Until then, `python -m bridge.eval` on its own is good for scoring a human or a CPU-vs-CPU set, and for testing the harness against `python -m bridge.fake_switch --matches N`.

## What "unattended" still needs

Phase 1's gate is a 50-game eval that runs unattended. Today it runs with **a human pressing rematch** between games: the harness prints "press rematch for the next game" and picks up the next match on its own. Everything after the first frame is automatic. What's missing for a truly hands-off run:

1. **Input path (another branch).** The bot has to actually be playing: `bridge.run --policy scripted` sending input packets, and the plugin applying them to the bot's port.
2. **One process for play and eval.** Wire `EvalSession.observers()` into `bridge.run`'s watcher (see above), or add a state fan-out so both can listen.
3. **Results screen to next match.** Needs menu automation, which doesn't exist. The plugin stops sending state on the results screen, so the PC can't see where it is. Options: (a) plugin-side, press A on the results screen and pick "rematch"/ready on character select through the same input-injection hook, ideally driven by a new "menu" packet from the PC; (b) use a ruleset/mode that loops by itself (for example a Training Modpack style auto-restart, or a custom mode that resets stocks instead of ending the match). Either way the plugin has to report the scene (results / CSS / stage select / in match) so the PC knows what to press.
4. **Character select state.** On rematch the CPU level, costume, and port assignment have to stay the same. The plugin should send both fighter kinds and CPU level in a handshake so the harness can refuse a wrong setup instead of logging it.
5. **Stage selection.** Same problem for alternating Battlefield and FD. Stage id is streamed now, so mixed-stage runs score correctly; picking the stage still needs menu automation.
6. **Recovery from hangs.** A watchdog on the PC side: if no state arrives for, say, 60 s outside the results screen, alert and stop. `--idle-timeout` covers the "Switch went away" case today; it can't tell "stuck in a menu" from "between games" until the plugin reports the scene.
7. **Game version and mod hash from the plugin.** The version check uses the heartbeat already. The mod hash is `--mod-hash` until SSB-10 has the plugin report it.
