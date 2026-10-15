import unittest

import pandas as pd

from cs2hl import detect

POV = "76561198000000001"


class FakeParser:
    def __init__(self, fires=(), hurts=(), views=None):
        self.fires, self.hurts, self.views = fires, hurts, views or {}

    def parse_event(self, name):
        if name == "weapon_fire":
            return pd.DataFrame({"tick": list(self.fires), "user_steamid": [POV] * len(self.fires)})
        return pd.DataFrame({"tick": [t for t, _ in self.hurts], "attacker_steamid": [POV] * len(self.hurts),
                             "user_steamid": [v for _, v in self.hurts]})

    def parse_ticks(self, props, ticks):
        rows = [(t, *self.views.get(t, (0.0, 0.0))) for t in ticks]
        return pd.DataFrame({"tick": [r[0] for r in rows], "pitch": [r[1] for r in rows],
                             "yaw": [r[2] for r in rows], "steamid": [POV] * len(rows)})


def kill(tick, weapon="ak47", victim="1"):
    return detect.Kill(tick, weapon, victim, False, [], victim)


class SpecialKillTest(unittest.TestCase):
    def test_spray_transfer_needs_continuous_fire(self):
        a, b = kill(1000), kill(1050, victim="2")
        detect.tag_special_kills(FakeParser(fires=range(990, 1051, 6)), POV, [a, b], {"spray_transfer"}, 64)
        self.assertIn("spray_transfer", a.flags)
        self.assertIn("spray_transfer", b.flags)

        a, b = kill(1000), kill(1050, victim="2")
        gap = [*range(990, 1001, 6), *range(1040, 1051, 6)]  # stopped shooting in between
        detect.tag_special_kills(FakeParser(fires=gap), POV, [a, b], {"spray_transfer"}, 64)
        self.assertEqual(a.flags + b.flags, [])

    def test_no_spray_transfer_with_pistol(self):
        a, b = kill(1000, "glock"), kill(1030, "glock", "2")
        detect.tag_special_kills(FakeParser(fires=range(990, 1031, 6)), POV, [a, b], {"spray_transfer"}, 64)
        self.assertEqual(a.flags + b.flags, [])

    def test_flick_measured_before_first_hit(self):
        # first hit at 2000 (kill at 2010); the view turned 50 degrees in the 0.2 s before it
        views = {1999 - 13: (0.0, 100.0), 1999: (0.0, 150.0)}
        k = kill(2010)
        detect.tag_special_kills(FakeParser(hurts=[(2000, "1"), (2010, "1")], views=views), POV, [k], {"flick"}, 64)
        self.assertEqual(k.flags, ["flick"])

    def test_slow_aim_is_not_a_flick(self):
        views = {1999 - 13: (0.0, 100.0), 1999: (0.0, 110.0)}
        k = kill(1999 + 1)
        detect.tag_special_kills(FakeParser(hurts=[(2000, "1")], views=views), POV, [k], {"flick"}, 64)
        self.assertEqual(k.flags, [])

    def test_angle_wraps_around(self):
        self.assertAlmostEqual(detect._angle((0, 170), (0, -170)), 20)


if __name__ == "__main__":
    unittest.main()


class PositionParser:
    """Fake demo with one smoke at the origin and the POV player walking out of it along x."""

    def __init__(self, path, victim_weapon="AK-47"):
        self.path, self.victim_weapon = path, victim_weapon  # path: tick -> x of the POV player

    def parse_event(self, name):
        if name == "smokegrenade_detonate":
            return pd.DataFrame({"tick": [0], "entityid": [7], "x": [0.0], "y": [0.0], "z": [0.0]})
        if name == "smokegrenade_expired":
            return pd.DataFrame({"tick": [64 * 20], "entityid": [7]})
        return pd.DataFrame()

    def parse_ticks(self, props, ticks):
        rows = []
        for t in ticks:
            rows.append((t, POV, self.path(t), "Desert Eagle"))
            rows.append((t, "1", 900.0, self.victim_weapon))
        return pd.DataFrame({"tick": [r[0] for r in rows], "steamid": [r[1] for r in rows], "X": [r[2] for r in rows],
                             "Y": [0.0] * len(rows), "Z": [0.0] * len(rows),
                             "active_weapon_name": [r[3] for r in rows]})


class PositionRuleTest(unittest.TestCase):
    def test_kill_after_walking_out_of_smoke(self):
        k = kill(640)  # inside the smoke until tick 600, then out
        detect.tag_special_kills(PositionParser(lambda t: 0.0 if t < 600 else 300.0), POV, [k], {"smoke_push"}, 64)
        self.assertEqual(k.flags, ["smoke_push"])

    def test_no_smoke_push_from_inside_or_far_away(self):
        inside, never = kill(640), kill(640)
        detect.tag_special_kills(PositionParser(lambda t: 0.0), POV, [inside], {"smoke_push"}, 64)
        detect.tag_special_kills(PositionParser(lambda t: 600.0), POV, [never], {"smoke_push"}, 64)
        self.assertEqual(inside.flags + never.flags, [])

    def test_pistol_headshot_against_rifle(self):
        hs = detect.Kill(640, "deagle", "1", True, [], "1")
        body = detect.Kill(640, "deagle", "1", False, [], "1")
        vs_pistol = detect.Kill(640, "deagle", "1", True, [], "1")
        far = lambda t: 600.0  # noqa: E731
        detect.tag_special_kills(PositionParser(far), POV, [hs, body], {"pistol_vs_rifle"}, 64)
        detect.tag_special_kills(PositionParser(far, "Glock-18"), POV, [vs_pistol], {"pistol_vs_rifle"}, 64)
        self.assertEqual((hs.flags, body.flags, vs_pistol.flags), (["pistol_vs_rifle"], [], []))
