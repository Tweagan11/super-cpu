# Logging schema (SSB-9)

One record per frame per player. One file per match, plus a JSON header.

## Match header

`match_id`, `started_at`, `game_version`, `mod_hash`, `stage_id`, `ruleset`, and for each port: `slot`, `fighter_kind`, `controller` (human name, `bot:<model>`, or `cpu:9`).

## Per-frame record

Everything in the state message (see `protocol.md`) for that player, plus the raw inputs that frame: `stick_x`, `stick_y`, `cstick_x`, `cstick_y`, `buttons`.

## Rules

- Files with a mismatched `game_version` or `mod_hash` are rejected at load.
- Raw logs are kept as-is forever. Features live in a separate derived step.
- Data lives in `data/`, which is git-ignored.
