import unittest
from pathlib import Path

from cs2hl.steam import parse_matches

FIXTURE = Path(__file__).parent / "fixtures/gcpd_matchhistory.html"
STEAMID = "76561198000000001"


class ParseMatchesTest(unittest.TestCase):
    def setUp(self):
        self.matches = parse_matches(FIXTURE.read_text(), "Ranked Competitive", STEAMID)

    def test_rows(self):
        self.assertEqual([m.map for m in self.matches], ["Dust II", "Mirage", "Train"])

    def test_win_when_on_second_team(self):
        m = self.matches[0]
        self.assertEqual((m.score, m.result), ("13 : 3", "win"))
        self.assertEqual((m.kills, m.assists, m.deaths), (9, 2, 9))
        self.assertEqual(m.id, "003800000000000000001_0000000001")
        self.assertEqual(m.time, "2026-10-12T23:24:35+00:00")
        self.assertEqual(m.duration, "26:34")

    def test_loss_when_on_second_team(self):
        m = self.matches[1]
        self.assertEqual((m.score, m.result, m.kills), ("8 : 13", "loss", 19))

    def test_first_team_and_expired_replay(self):
        m = self.matches[2]
        self.assertEqual((m.score, m.result, m.url), ("13 : 3", "win", ""))


if __name__ == "__main__":
    unittest.main()
