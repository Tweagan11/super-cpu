"""Stages for the v1 matchup (SSB-8): Battlefield and Final Destination, Omega
forms included (Omega uses the FD layout).

Two halves:

- Stage ids. `stage_id` in the state packet is what the game's
  `app::sv_information::stage_id()` returns (plugin/src/state.rs). The name
  table is generated from skyline-smash lua_const.rs (`StageID`). The Training
  Modpack groups ids the same way in `src/training/tech.rs`: `End_*` ids are
  the Omega (FD layout) forms of each stage, `Battle_*` ids the Battlefield
  forms. Note 0 is a real id: plain Battlefield.
- Layout geometry, used by the scripted bot and the features. Battlefield is
  measured on hardware with `python -m bridge.measure`; FD is still a guess.

`resolve_layout(stage_id, fallback)` is the one place that picks a layout: the
streamed stage id when it maps to a layout we know, else the --stage flag.
Use STAGE_ID_UNKNOWN (-1) for "no stage id available" (old logs, tests).
"""
from __future__ import annotations

from dataclasses import dataclass

STAGE_NAMES: dict[int, str] = {
    0: "BattleField",
    1: "End_BattleField",
    2: "BattleField_L",
    3: "End",
    4: "Battle_End",
    5: "Mario_Castle64",
    6: "End_Mario_Castle64",
    7: "Battle_Mario_Castle64",
    8: "DK_Jungle",
    9: "End_DK_Jungle",
    10: "Battle_DK_Jungle",
    11: "Zelda_Hyrule",
    12: "End_Zelda_Hyrule",
    13: "Battle_Zelda_Hyrule",
    14: "Yoshi_Story",
    15: "End_Yoshi_Story",
    16: "Battle_Yoshi_Story",
    17: "Kirby_Pupupu64",
    18: "End_Kirby_Pupupu64",
    19: "Battle_Kirby_Pupupu64",
    20: "Poke_Yamabuki",
    21: "End_Poke_Yamabuki",
    22: "Battle_Poke_Yamabuki",
    23: "Mario_Past64",
    24: "End_Mario_Past64",
    25: "Battle_Mario_Past64",
    26: "Mario_CastleDx",
    27: "End_Mario_CastleDx",
    28: "Battle_Mario_CastleDx",
    29: "Mario_Rainbow",
    30: "End_Mario_Rainbow",
    31: "Battle_Mario_Rainbow",
    32: "DK_WaterFall",
    33: "End_DK_WaterFall",
    34: "Battle_DK_WaterFall",
    35: "DK_Lodge",
    36: "End_DK_Lodge",
    37: "Battle_DK_Lodge",
    38: "Zelda_Greatbay",
    39: "End_Zelda_Greatbay",
    40: "Battle_Zelda_Greatbay",
    41: "Zelda_Temple",
    42: "End_Zelda_Temple",
    43: "Battle_Zelda_Temple",
    44: "Yoshi_CartBoard",
    45: "End_Yoshi_CartBoard",
    46: "Battle_Yoshi_CartBoard",
    47: "Yoshi_Yoster",
    48: "End_Yoshi_Yoster",
    49: "Battle_Yoshi_Yoster",
    50: "Kirby_Fountain",
    51: "End_Kirby_Fountain",
    52: "Battle_Kirby_Fountain",
    53: "Kirby_Greens",
    54: "End_Kirby_Greens",
    55: "Battle_Kirby_Greens",
    56: "Fox_Corneria",
    57: "End_Fox_Corneria",
    58: "Battle_Fox_Corneria",
    59: "Fox_Venom",
    60: "End_Fox_Venom",
    61: "Battle_Fox_Venom",
    62: "Metroid_ZebesDx",
    63: "End_Metroid_ZebesDx",
    64: "Battle_Metroid_ZebesDx",
    65: "Mother_Onett",
    66: "End_Mother_Onett",
    67: "Battle_Mother_Onett",
    68: "Poke_Stadium",
    69: "End_Poke_Stadium",
    70: "Battle_Poke_Stadium",
    71: "Metroid_Kraid",
    72: "End_Metroid_Kraid",
    73: "Battle_Metroid_Kraid",
    74: "Mother_Fourside",
    75: "End_Mother_Fourside",
    76: "Battle_Mother_Fourside",
    77: "Fzero_Bigblue",
    78: "End_Fzero_Bigblue",
    79: "Battle_Fzero_Bigblue",
    80: "Mario_PastUsa",
    81: "End_Mario_PastUsa",
    82: "Battle_Mario_PastUsa",
    83: "Mario_Dolpic",
    84: "End_Mario_Dolpic",
    85: "Battle_Mario_Dolpic",
    86: "Yoshi_Island",
    87: "End_Yoshi_Island",
    88: "Battle_Yoshi_Island",
    89: "Fox_LylatCruise",
    90: "End_Fox_LylatCruise",
    91: "Battle_Fox_LylatCruise",
    92: "Zelda_Oldin",
    93: "End_Zelda_Oldin",
    94: "Battle_Zelda_Oldin",
    95: "Animal_Village",
    96: "End_Animal_Village",
    97: "Battle_Animal_Village",
    98: "Icarus_SkyWorld",
    99: "End_Icarus_SkyWorld",
    100: "Battle_Icarus_SkyWorld",
    101: "FE_Siege",
    102: "End_FE_Siege",
    103: "Battle_FE_Siege",
    104: "Wario_Madein",
    105: "End_Wario_Madein",
    106: "Battle_Wario_Madein",
    107: "Poke_Stadium2",
    108: "End_Poke_Stadium2",
    109: "Battle_Poke_Stadium2",
    110: "Kirby_Halberd",
    111: "End_Kirby_Halberd",
    112: "Battle_Kirby_Halberd",
    113: "MG_Shadowmoses",
    114: "End_MG_Shadowmoses",
    115: "Battle_MG_Shadowmoses",
    116: "Mother_Newpork",
    117: "End_Mother_Newpork",
    118: "Battle_Mother_Newpork",
    119: "Ice_Top",
    120: "End_Ice_Top",
    121: "Battle_Ice_Top",
    122: "Metroid_Norfair",
    123: "End_Metroid_Norfair",
    124: "Battle_Metroid_Norfair",
    125: "Kart_CircuitX",
    126: "End_Kart_CircuitX",
    127: "Battle_Kart_CircuitX",
    128: "Metroid_Orpheon",
    129: "End_Metroid_Orpheon",
    130: "Battle_Metroid_Orpheon",
    131: "Pikmin_Planet",
    132: "End_Pikmin_Planet",
    133: "Battle_Pikmin_Planet",
    134: "Mario_PastX",
    135: "End_Mario_PastX",
    136: "Battle_Mario_PastX",
    137: "Fzero_Porttown",
    138: "End_Fzero_Porttown",
    139: "Battle_Fzero_Porttown",
    140: "LuigiMansion",
    141: "End_LuigiMansion",
    142: "Battle_LuigiMansion",
    143: "Zelda_Pirates",
    144: "End_Zelda_Pirates",
    145: "Battle_Zelda_Pirates",
    146: "Poke_Tengam",
    147: "End_Poke_Tengam",
    148: "Battle_Poke_Tengam",
    149: "_75m",
    150: "End_75m",
    151: "Battle_75m",
    152: "MarioBros",
    153: "End_MarioBros",
    154: "Battle_MarioBros",
    155: "Plankton",
    156: "End_Plankton",
    157: "Battle_Plankton",
    158: "Sonic_Greenhill",
    159: "End_Sonic_Greenhill",
    160: "Battle_Sonic_Greenhill",
    161: "Mario_3DLand",
    162: "End_Mario_3DLand",
    163: "Battle_Mario_3DLand",
    164: "Mario_NewBros2",
    165: "End_Mario_NewBros2",
    166: "Battle_Mario_NewBros2",
    167: "Mario_Paper",
    168: "End_Mario_Paper",
    169: "Battle_Mario_Paper",
    170: "Zelda_Gerudo",
    171: "End_Zelda_Gerudo",
    172: "Battle_Zelda_Gerudo",
    173: "Zelda_Train",
    174: "End_Zelda_Train",
    175: "Battle_Zelda_Train",
    176: "Poke_Unova",
    177: "End_Poke_Unova",
    178: "Battle_Poke_Unova",
    179: "Poke_Tower",
    180: "End_Poke_Tower",
    181: "Battle_Poke_Tower",
    182: "FE_Arena",
    183: "End_FE_Arena",
    184: "Battle_FE_Arena",
    185: "Icarus_Uprising",
    186: "End_Icarus_Uprising",
    187: "Battle_Icarus_Uprising",
    188: "Animal_Island",
    189: "End_Animal_Island",
    190: "Battle_Animal_Island",
    191: "PunchOutSB",
    192: "End_PunchOutSB",
    193: "Battle_PunchOutSB",
    194: "PunchOutW",
    195: "End_PunchOutW",
    196: "Battle_PunchOutW",
    197: "Xeno_Gaur",
    198: "End_Xeno_Gaur",
    199: "Battle_Xeno_Gaur",
    200: "Nintendogs",
    201: "End_Nintendogs",
    202: "Battle_Nintendogs",
    203: "StreetPass",
    204: "End_StreetPass",
    205: "Battle_StreetPass",
    206: "Tomodachi",
    207: "End_Tomodachi",
    208: "Battle_Tomodachi",
    209: "Pictochat2",
    210: "End_Pictochat2",
    211: "Battle_Pictochat2",
    212: "Rock_Wily",
    213: "End_Rock_Wily",
    214: "Battle_Rock_Wily",
    215: "Mother_Magicant",
    216: "End_Mother_Magicant",
    217: "Battle_Mother_Magicant",
    218: "Kirby_Gameboy",
    219: "End_Kirby_Gameboy",
    220: "Battle_Kirby_Gameboy",
    221: "BalloonFight",
    222: "End_BalloonFight",
    223: "Battle_BalloonFight",
    224: "Fzero_Mutecity3DS",
    225: "End_Fzero_Mutecity3DS",
    226: "Battle_Fzero_Mutecity3DS",
    227: "Mario_Uworld",
    228: "End_Mario_Uworld",
    229: "Battle_Mario_Uworld",
    230: "Mario_Galaxy",
    231: "End_Mario_Galaxy",
    232: "Battle_Mario_Galaxy",
    233: "Kart_CircuitFor",
    234: "End_Kart_CircuitFor",
    235: "Battle_Kart_CircuitFor",
    236: "Zelda_Skyward",
    237: "End_Zelda_Skyward",
    238: "Battle_Zelda_Skyward",
    239: "Kirby_Cave",
    240: "End_Kirby_Cave",
    241: "Battle_Kirby_Cave",
    242: "Poke_Kalos",
    243: "End_Poke_Kalos",
    244: "Battle_Poke_Kalos",
    245: "FE_Colloseum",
    246: "End_FE_Colloseum",
    247: "Battle_FE_Colloseum",
    248: "Icarus_Angeland",
    249: "End_Icarus_Angeland",
    250: "Battle_Icarus_Angeland",
    251: "Wario_Gamer",
    252: "End_Wario_Gamer",
    253: "Battle_Wario_Gamer",
    254: "Pikmin_Garden",
    255: "End_Pikmin_Garden",
    256: "Battle_Pikmin_Garden",
    257: "Animal_City",
    258: "End_Animal_City",
    259: "Battle_Animal_City",
    260: "WiiFit",
    261: "End_WiiFit",
    262: "Battle_WiiFit",
    263: "WreckingCrew",
    264: "End_WreckingCrew",
    265: "Battle_WreckingCrew",
    266: "Pilotwings",
    267: "End_Pilotwings",
    268: "Battle_Pilotwings",
    269: "WufuIsland",
    270: "End_WufuIsland",
    271: "Battle_WufuIsland",
    272: "Sonic_Windyhill",
    273: "End_Sonic_Windyhill",
    274: "Battle_Sonic_Windyhill",
    275: "Pac_Land",
    276: "End_Pac_Land",
    277: "Battle_Pac_Land",
    278: "FlatZoneX",
    279: "End_FlatZoneX",
    280: "Battle_FlatZoneX",
    281: "DuckHunt",
    282: "End_DuckHunt",
    283: "Battle_DuckHunt",
    284: "SF_Suzaku",
    285: "End_SF_Suzaku",
    286: "Battle_SF_Suzaku",
    287: "Mario_Maker",
    288: "End_Mario_Maker",
    289: "Battle_Mario_Maker",
    290: "FF_Midgar",
    291: "End_FF_Midgar",
    292: "Battle_FF_Midgar",
    293: "Bayo_Clock",
    294: "End_Bayo_Clock",
    295: "Battle_Bayo_Clock",
    296: "Spla_Parking",
    297: "End_Spla_Parking",
    298: "Battle_Spla_Parking",
    299: "Dracula_Castle",
    300: "End_Dracula_Castle",
    301: "Battle_Dracula_Castle",
    302: "Zelda_Tower",
    303: "End_Zelda_Tower",
    304: "Battle_Zelda_Tower",
    305: "Mario_Odyssey",
    306: "End_Mario_Odyssey",
    307: "Battle_Mario_Odyssey",
    308: "Training",
    309: "SettingStage",
    310: "ResultStage",
    311: "ShamFight",
    312: "SpiritsRoulette",
    313: "CampaignMap",
    314: "BonusGame",
    315: "HomerunContest",
    316: "Staffroll",
    317: "BossStage_GanonBoss",
    318: "BossStage_Rathalos",
    319: "BossStage_Marx",
    320: "BossStage_Dracula",
    321: "BossStage_Galleom",
    322: "BossStage_Final1",
    323: "BossStage_Final2",
    324: "BossStage_Final3",
    325: "SP_Edit",
    326: "PhotoStage",
    327: "TestStage",
    328: "Jack_Mementoes",
    329: "End_Jack_Mementoes",
    330: "Battle_Jack_Mementoes",
    331: "ResultStage_Jack",
    332: "Brave_Altar",
    333: "End_Brave_Altar",
    334: "Battle_Brave_Altar",
    335: "Buddy_Spiral",
    336: "End_Buddy_Spiral",
    337: "Battle_Buddy_Spiral",
    338: "Dolly_Stadium",
    339: "End_Dolly_Stadium",
    340: "Battle_Dolly_Stadium",
    341: "FE_Shrine",
    342: "End_FE_Shrine",
    343: "Battle_FE_Shrine",
    344: "Tantan_Spring",
    345: "End_Tantan_Spring",
    346: "Battle_Tantan_Spring",
    347: "BattleField_S",
    348: "Pickel_World",
    349: "End_Pickel_World",
    350: "Battle_Pickel_World",
    351: "FF_Cave",
    352: "End_FF_Cave",
    353: "Battle_FF_Cave",
    354: "ResultStage_Edge",
    355: "Xeno_Alst",
    356: "End_Xeno_Alst",
    357: "Battle_Xeno_Alst",
    358: "Demon_Dojo",
    359: "End_Demon_Dojo",
    360: "Battle_Demon_Dojo",
    361: "Trail_Castle",
    362: "End_Trail_Castle",
    363: "Battle_Trail_Castle",
}

STAGE_ID_UNKNOWN = -1
STAGE_ID_BATTLEFIELD = 0x0
STAGE_ID_FINAL_DESTINATION = 0x3  # "End" internally

# Layout names. Big Battlefield (BattleField_L) and Small Battlefield
# (BattleField_S) are different layouts and belong to neither group.
LAYOUT_FD = "fd"
LAYOUT_BATTLEFIELD = "battlefield"


def stage_name(stage_id: int) -> str:
    return STAGE_NAMES.get(stage_id, f"stage {stage_id}")


def stage_layout(stage_id: int) -> str | None:
    """'fd' for Final Destination and every Omega form, 'battlefield' for
    Battlefield and every Battlefield form, None for anything else."""
    if stage_id in STAGE_ID_LAYOUTS:
        return STAGE_ID_LAYOUTS[stage_id]
    name = STAGE_NAMES.get(stage_id)
    if name is None:
        return None
    if name == "End" or name.startswith("End_"):
        return LAYOUT_FD
    if name == "BattleField" or name.startswith("Battle_"):
        return LAYOUT_BATTLEFIELD
    return None


@dataclass(frozen=True)
class Platform:
    left: float
    right: float
    y: float


@dataclass(frozen=True)
class StageLayout:
    name: str
    center_x: float = 0.0
    ledge_x: float = 80.0      # |x| of the main stage ledges, measured from center_x
    ground_y: float = 0.0      # main stage surface
    platforms: tuple[Platform, ...] = ()

    def offstage(self, x: float, margin: float = 0.0) -> bool:
        return abs(x - self.center_x) > self.ledge_x - margin

    def below_stage(self, y: float) -> bool:
        return y < self.ground_y


# Measured on hardware with bridge.measure (13.0.4, R.O.B., 2026-10-16).
# Positions are where a fighter's origin can stand, so the real ledge is a bit further out.
BATTLEFIELD = StageLayout(
    name=LAYOUT_BATTLEFIELD,
    center_x=-0.3,
    ledge_x=79.6,
    ground_y=0.0,
    platforms=(
        Platform(-58.5, -25.4, 24.1),
        Platform(25.0, 59.4, 24.1),
        Platform(-16.6, 17.2, 47.2),
    ),
)
# TODO(measure): FD is still an estimate.
FINAL_DESTINATION = StageLayout(name=LAYOUT_FD, ledge_x=85.0)

LAYOUTS: dict[str, StageLayout] = {
    LAYOUT_BATTLEFIELD: BATTLEFIELD,
    LAYOUT_FD: FINAL_DESTINATION,
}
# Accepted spellings for --stage. Omega forms share the FD layout.
ALIASES: dict[str, str] = {
    "bf": LAYOUT_BATTLEFIELD,
    "battlefield": LAYOUT_BATTLEFIELD,
    "fd": LAYOUT_FD,
    "final_destination": LAYOUT_FD,
    "omega": LAYOUT_FD,
}

# Overrides for specific stage ids, checked before the name-based grouping.
# Empty by default; put an id here if hardware shows the grouping is wrong.
STAGE_ID_LAYOUTS: dict[int, str] = {}


def layout_by_name(name: str) -> StageLayout:
    key = ALIASES.get(name.lower())
    if key is None:
        raise ValueError(f"unknown stage {name!r}, use one of {sorted(ALIASES)}")
    return LAYOUTS[key]


def resolve_layout(stage_id: int, fallback: StageLayout) -> StageLayout:
    """Layout for a streamed stage id, or the flag/config fallback when the id
    is unknown or not a Battlefield/FD form."""
    name = stage_layout(stage_id)
    return LAYOUTS[name] if name else fallback


def layout_name(stage_id: int, fallback: str) -> str:
    """Layout name for logs and eval rows."""
    return resolve_layout(stage_id, layout_by_name(fallback)).name
