# Focused training: curriculum and drills (Phase 6)

Board: epic KAN-38, stories SSB-30 to SSB-36 (KAN-39 to KAN-45).

## Why

RL against random level 9 CPUs learns slowly for two reasons:

- **Most of a match isn't practice.** The bot is worst at recovering,
  edgeguarding and the damage trade (~50% dealt vs ~145% taken per minute,
  2026-10-20). Those situations take a few seconds of a ~100 s match.
- **It almost never wins** (7 wins in ~1,200 matches), so it rarely sees
  what winning looks like.

## Part 1: curriculum (SSB-30)

Game settings, no code:

- One opponent character on the CPU card (start with Mario), not Random.
- Final Destination only.
- CPU level 6 or 7. Step up one level once the bot wins about half its
  games over 40+ matches. Pass `--opponent cpu7` (etc.) so logs record it.
- Add a second opponent character only after reaching level 9 with the first.

Code support (SSB-30): `opp_fighter` and `opponent` columns in the RL
`matches.csv`, rolling win rate per opponent in the log, and a step-up
message past the threshold.

## Part 2: drills

Set up the situation the bot needs to practice, play a short episode, reset,
repeat. Dozens of recovery attempts a minute instead of a couple per match.

### Where drills run (SSB-31 decides)

Training mode: infinite stocks, so a failed attempt costs nothing. The bot
is the human slot (P1), driven through the final input mapping hook (proven
on a human port in versus). The training CPU is the opponent. Settings for a
drill session: `bot_port = 1`, `drive_cpu = false`.

Fallback if training mode doesn't work out: versus with 99 stocks and
`drills_in_versus = true`.

Hardware questions first (SSB-31): does state stream normally in training
mode, does driving P1 work there, does this version's training menu have a
CPU behavior with a level, and is the Training Modpack installed (its save
states use the same game calls as our reset).

### Reset packet (SSB-32)

PC to Switch, kind 9: per port position, facing, damage, jumps left, start
kind (ground / air / ledge), optional launch speed. Applied on the game
thread in the fighter update, in the Training Modpack's load order
(`src/training/save_states.rs`):

1. `KineticModule::clear_speed_all`
2. `PostureModule::set_pos`, `PostureModule::set_lr`
3. `StatusModule::change_status_request` to WAIT (ground) or FALL (air)
4. damage: `DamageModule::heal(-current)` then `DamageModule::add_damage`
5. jumps: the jump count work int

If a fighter is in hitstun or dead, kill-then-place like the modpack does
(`change_status_force(DEAD)`, wait for alive, then place). Nana is skipped.
The state packet echoes the last reset seq applied so the PC knows when the
new situation is live.

### Drill runner (SSB-33)

`bridge.run --drill recovery`. A drill is a start sampler, an end rule and a
short reward:

| drill | start | success | fail | timeout |
| --- | --- | --- | --- | --- |
| recovery | R.O.B. 30 to 120 past the ledge, -80 to +40 high, 0 to 2 jumps, 0 to 120%; opponent on stage near the ledge | on stage or holding the ledge | KO | 8 s |
| edgeguard | opponent offstage; R.O.B. on stage near the ledge | opponent KO | opponent back on stage | 8 s |

Logs: npz per block of episodes, plus `data/drills/<run>/episodes.csv`.

### RL on drills (SSB-34)

Same run and checkpoint as matches, so sessions can alternate: matches, a
drill block, matches. Success and fail are terminal; timeouts bootstrap from
the value head. Updates every N episodes.

### Proof it carries over (SSB-35)

Drill success rate before and after training, then `sd_report` on 50 matches
before and after: self-destructs per match and "never used up-B" should drop
without damage dealt per minute getting worse.

### Next drills (SSB-36)

Edgeguard, then disadvantage (escaping combos, aimed at the damage trade),
then ledge options.

## Order

SSB-30 and SSB-31 now (independent). Then SSB-32, SSB-33, SSB-34 in order,
then SSB-35. SSB-36 after recovery is proven.
