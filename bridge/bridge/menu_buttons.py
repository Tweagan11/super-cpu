"""Button bits for the menu press packet (kind 8).

Not the same bits as buttons.py. Menus read the game's raw Controller struct
(`ButtonBitfield`, Training Modpack `src/common/input.rs`), so these are
controller buttons, not mapped fighter inputs. Bit order is the struct's
field order, lowest bit first.
"""

DPAD_UP = 0x1
DPAD_RIGHT = 0x2
DPAD_DOWN = 0x4
DPAD_LEFT = 0x8
X = 0x10
A = 0x20
B = 0x40
Y = 0x80
L = 0x100
R = 0x200
ZL = 0x400
ZR = 0x800
STICK_L = 0x10000
STICK_R = 0x20000
PLUS = 0x40000
MINUS = 0x80000
