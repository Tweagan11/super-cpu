"""Button bits for the input packet's `buttons` field.

These are the game's mapped-input bits (what a controller press becomes after
the player's button config), taken from the Training Modpack's `Buttons`
bitflags in `src/common/input.rs` (`MappedInputs.buttons`). The plugin writes
this value straight into the bot's MappedInputs, so these must stay in sync
with that layout.

Use the names, never raw numbers, in policies.
"""

ATTACK = 0x1
SPECIAL = 0x2
JUMP = 0x4
GUARD = 0x8
CATCH = 0x10
SMASH = 0x20
JUMP_MINI = 0x40
CSTICK_ON = 0x80
STOCK_SHARE = 0x100
ATTACK_RAW = 0x200
APPEAL_HI = 0x400
SPECIAL_RAW = 0x800
APPEAL_LW = 0x1000
APPEAL_SL = 0x2000
APPEAL_SR = 0x4000
FLICK_JUMP = 0x8000
GUARD_HOLD = 0x10000
SPECIAL_RAW2 = 0x20000

NONE = 0

# Stick values are i8, -128..127. Full tilt is 127.
STICK_MAX = 127
