"""SSB-3 remaining fields, stage ids, and the SSB-10 version guard (PC side)."""
import unittest
from dataclasses import replace

from bridge.fake_switch import JUMPS_MAX, SHIELD_MAX, fake_fighter
from bridge.protocol import Fighter, Frame
from bridge.stages import (
    STAGE_ID_BATTLEFIELD as BATTLEFIELD,
    STAGE_ID_FINAL_DESTINATION as FINAL_DESTINATION,
    LAYOUT_BATTLEFIELD,
    LAYOUT_FD,
    STAGE_NAMES,
    stage_layout,
    stage_name,
)
from bridge.udp_listen import describe, stage_line
from bridge.wire import pack_heartbeat, pack_state, unpack_heartbeat, unpack_state

# Bytes from plugin/src/net.rs::state_packet (v3: plus a zero capture tail; the
# full v3 golden with inputs is in test_capture.py), built on host stable Rust with
# stub smash/skyline types (scratch crate, see the SSB-3 commit message). Same
# values as rust_frame() below. If the Rust layout changes, regenerate this.
RUST_STATE_HEX = (
    "534350550300010009000000d2040000010000002d00000000004841000050400000c0bf"
    "0000304000002a42000016420300000047000000000000002301efcdab00000000008040"
    "ffffffff01000000030000002d000000000048c1000050400000c03f0000304000002041"
    "000016420300000047000000000000002301efcdab00000000008040ffffffff01000000"
    "03000000"
    # v3 tail: input_flags 0, both ports uncaptured (zeros)
    "0000000000000000000000000000000000000000"
)


def rust_fighter(dmg: float, side: float) -> Fighter:
    return Fighter(
        fighter_kind=0x2D, pos_x=12.5 * side, pos_y=3.25, vel_x=-1.5 * side, vel_y=2.75,
        damage=dmg, shield_hp=37.5, stocks=3, status_kind=0x47, motion_kind=0xABCDEF0123,
        anim_frame=4.0, facing=-1, jumps_left=1, flags=0b11,
    )


def rust_frame() -> Frame:
    return Frame(1234, 1, rust_fighter(42.5, 1.0), rust_fighter(10.0, -1.0))


class RustBytesTests(unittest.TestCase):
    def test_new_fields_match_rust_bytes(self):
        self.assertEqual(pack_state(9, rust_frame()).hex(), RUST_STATE_HEX)

    def test_decode_rust_bytes(self):
        _, f = unpack_state(bytes.fromhex(RUST_STATE_HEX))
        self.assertEqual(f.stage_id, 1)
        self.assertEqual((f.bot.vel_x, f.bot.vel_y), (-1.5, 2.75))
        self.assertEqual((f.opponent.vel_x, f.opponent.vel_y), (1.5, 2.75))
        self.assertEqual(f.bot.shield_hp, 37.5)
        self.assertEqual(f.bot.jumps_left, 1)
        self.assertTrue(f.bot.airborne)
        self.assertTrue(f.bot.in_hitstun)
        self.assertFalse(f.bot.invincible)


class FlagTests(unittest.TestCase):
    def _f(self, flags):
        return replace(rust_fighter(0.0, 1.0), flags=flags)

    def test_flag_bits(self):
        self.assertEqual([self._f(1).airborne, self._f(1).in_hitstun, self._f(1).invincible], [True, False, False])
        self.assertEqual([self._f(2).airborne, self._f(2).in_hitstun, self._f(2).invincible], [False, True, False])
        self.assertEqual([self._f(4).airborne, self._f(4).in_hitstun, self._f(4).invincible], [False, False, True])


class DescribeTests(unittest.TestCase):
    def test_shows_new_fields(self):
        line = describe(1, rust_fighter(42.5, 1.0))
        self.assertIn("v(-1.50, +2.75)", line)
        self.assertIn("sh 37.5", line)
        self.assertIn("j1", line)
        self.assertIn("HITSTUN", line)
        self.assertNotIn("INVINC", line)
        self.assertIn("DAMAGE", line)

    def test_no_markers_when_clear(self):
        f = Fighter(0x2D, 0, 0, 0, 0, 0, 50, 3, 0, 0, 0, 1, 2, 0)
        line = describe(2, f)
        self.assertNotIn("HITSTUN", line)
        self.assertNotIn("INVINC", line)
        self.assertIn("ground", line)

    def test_invincible_marker(self):
        f = Fighter(0x2D, 0, 0, 0, 0, 0, 50, 3, 0, 0, 0, 1, 2, 4)
        self.assertIn("INVINC", describe(1, f))


class StageTests(unittest.TestCase):
    def test_known_ids(self):
        # skyline-smash lua_const.rs StageID
        self.assertEqual(STAGE_NAMES[BATTLEFIELD], "BattleField")
        self.assertEqual(STAGE_NAMES[FINAL_DESTINATION], "End")
        self.assertEqual(stage_name(0x1), "End_BattleField")
        self.assertEqual(stage_name(0x4), "Battle_End")
        self.assertEqual(stage_name(9999), "stage 9999")

    def test_layouts(self):
        self.assertEqual(stage_layout(BATTLEFIELD), LAYOUT_BATTLEFIELD)
        self.assertEqual(stage_layout(FINAL_DESTINATION), LAYOUT_FD)
        # Omega forms are End_*, Battlefield forms are Battle_*
        self.assertEqual(stage_layout(0x1), LAYOUT_FD)  # End_BattleField (Omega Battlefield)
        self.assertEqual(stage_layout(0x9), LAYOUT_FD)  # End_DK_Jungle
        self.assertEqual(stage_layout(0x4), LAYOUT_BATTLEFIELD)  # Battle_End
        self.assertEqual(stage_layout(0xA), LAYOUT_BATTLEFIELD)  # Battle_DK_Jungle
        # Big and Small Battlefield are different layouts
        self.assertIsNone(stage_layout(0x2))  # BattleField_L
        self.assertIsNone(stage_layout(0x15B))  # BattleField_S
        self.assertIsNone(stage_layout(0x8))  # DK_Jungle, normal form
        self.assertIsNone(stage_layout(9999))

    def test_every_omega_and_bf_form_is_grouped(self):
        for sid, name in STAGE_NAMES.items():
            if name.startswith("End_"):
                self.assertEqual(stage_layout(sid), LAYOUT_FD, name)
            elif name.startswith("Battle_"):
                self.assertEqual(stage_layout(sid), LAYOUT_BATTLEFIELD, name)

    def test_stage_line(self):
        self.assertIn("End_BattleField", stage_line(1))
        self.assertIn("layout fd", stage_line(1))
        self.assertIn("layout other", stage_line(0x2))


class FakeSwitchTests(unittest.TestCase):
    def test_values_are_plausible(self):
        seen_hitstun = seen_shield = seen_air = False
        jumps = set()
        for i in range(60 * 6):
            for side in (-1, 1):
                f = fake_fighter(i / 60, side)
                self.assertTrue(0 <= f.shield_hp <= SHIELD_MAX)
                self.assertTrue(0 <= f.jumps_left <= JUMPS_MAX)
                self.assertLess(abs(f.vel_x), 2)
                self.assertLess(abs(f.vel_y), 2)
                if not f.airborne:
                    self.assertEqual(f.jumps_left, JUMPS_MAX)
                    self.assertEqual(f.vel_y, 0.0)
                jumps.add(f.jumps_left)
                seen_hitstun |= f.in_hitstun
                seen_air |= f.airborne
                seen_shield |= f.shield_hp < SHIELD_MAX
                self.assertFalse(f.invincible)
        self.assertTrue(seen_hitstun and seen_shield and seen_air)
        self.assertEqual(jumps, {0, 1, 2})

    def test_round_trips(self):
        f = Frame(5, BATTLEFIELD, fake_fighter(1.3, -1), fake_fighter(1.3, 1))
        _, back = unpack_state(pack_state(0, f))
        self.assertEqual(back.stage_id, BATTLEFIELD)
        self.assertAlmostEqual(back.bot.vel_x, f.bot.vel_x, places=5)
        self.assertAlmostEqual(back.bot.shield_hp, f.bot.shield_hp, places=5)
        self.assertEqual(back.opponent.jumps_left, f.opponent.jumps_left)


class VersionTests(unittest.TestCase):
    def test_running_version_round_trips(self):
        # The plugin now puts the version the Switch is really running here.
        self.assertEqual(unpack_heartbeat(pack_heartbeat(1, 1, "13.0.3")).game_version, "13.0.3")
        self.assertEqual(unpack_heartbeat(pack_heartbeat(1, 1, "")).game_version, "")

    def test_fake_switch_sends_no_state_on_mismatch(self):
        import socket
        from bridge.fake_switch import run
        from bridge.wire import KIND_STATE, unpack_header

        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(0.2)
        port = rx.getsockname()[1]
        run("127.0.0.1", port, 0.0, "13.0.3", True, 0.3)
        kinds = []
        while True:
            try:
                kinds.append(unpack_header(rx.recv(2048)).kind)
            except socket.timeout:
                break
        rx.close()
        self.assertTrue(kinds)
        self.assertNotIn(KIND_STATE, kinds)


if __name__ == "__main__":
    unittest.main()
