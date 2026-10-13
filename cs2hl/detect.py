"""Find highlight moments for the local player in a CS2 demo."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from demoparser2 import DemoParser

TEAM_T, TEAM_CT = 2, 3
WINNER_TEAM = {"T": TEAM_T, "CT": TEAM_CT, 2: TEAM_T, 3: TEAM_CT}
MULTIKILL_NAMES = {2: "2k", 3: "3k", 4: "4k", 5: "ace"}
MULTIKILL_SCORE = {3: 3.0, 4: 5.0, 5: 8.0}


@dataclass
class Kill:
    tick: int
    weapon: str
    victim: str
    headshot: bool
    flags: list[str]


@dataclass
class Highlight:
    round: int
    kills: list[Kill]
    clutch_vs: int = 0  # 1vN won, 0 if not a clutch
    score: float = 0.0
    # (start_tick, end_tick) windows; several when kills are far apart so dead time gets cut
    sequences: list[tuple[int, int]] = field(default_factory=list)

    @property
    def label(self) -> str:
        parts = []
        if self.clutch_vs:
            parts.append(f"1v{self.clutch_vs}")
        n = len(self.kills)
        if n >= 2 or not self.clutch_vs:
            parts.append(MULTIKILL_NAMES.get(n, f"{n}k"))
        weapons = {k.weapon for k in self.kills}
        if len(weapons) == 1:
            parts.append(weapons.pop())
        return "_".join(parts)

    @property
    def name(self) -> str:
        return f"r{self.round:02d}_{self.label}"


@dataclass
class DemoInfo:
    path: Path
    map_name: str
    tickrate: int
    pov_steamid: str
    pov_name: str
    highlights: list[Highlight]


def _flags(row) -> list[str]:
    flags = []
    if row.get("noscope"):
        flags.append("noscope")
    if row.get("thrusmoke"):
        flags.append("smoke")
    if row.get("penetrated"):
        flags.append("wallbang")
    if row.get("attackerinair"):
        flags.append("jump")
    if row.get("attackerblind"):
        flags.append("blind")
    return flags


def _windows(ticks: list[int], tickrate: int, pre: float, post: float, gap: float,
             end_cap: int | None) -> list[tuple[int, int]]:
    """Group kill ticks into clip windows, splitting where the gap is too long."""
    groups: list[list[int]] = [[ticks[0]]]
    for t in ticks[1:]:
        if t - groups[-1][-1] > gap * tickrate:
            groups.append([t])
        else:
            groups[-1].append(t)
    out = []
    for g in groups:
        start = max(1, int(g[0] - pre * tickrate))
        end = int(g[-1] + post * tickrate)
        out.append((start, end))
    if end_cap is not None and out[-1][1] < end_cap:
        out[-1] = (out[-1][0], end_cap)
    return out


def analyze(demo: Path, steamids: list[str], *, min_kills: int = 3, clutch_min_enemies: int = 2,
            pre_seconds: float = 3.0, post_seconds: float = 2.0,
            max_gap_seconds: float = 8.0) -> DemoInfo:
    parser = DemoParser(str(demo))
    header = parser.parse_header()

    players = parser.parse_player_info()
    present = set(players["steamid"].astype(str))
    pov = next((s for s in steamids if s in present), None)
    if pov is None:
        raise ValueError(f"none of {steamids} played in {demo.name}")
    pov_name = str(players.loc[players["steamid"].astype(str) == pov, "name"].iloc[0])

    tickrate = 64
    deaths = parser.parse_event(
        "player_death", player=["team_num"], other=["total_rounds_played", "is_warmup_period"]
    )
    deaths = deaths[~deaths["is_warmup_period"].astype(bool)].sort_values("tick")
    for col in ("attacker_steamid", "user_steamid"):
        deaths[col] = deaths[col].astype(str)
    round_ends = parser.parse_event("round_end")
    freeze_ends = parser.parse_event("round_freeze_end")

    end_tick_by_round = {int(r["round"]): int(r["tick"]) for _, r in round_ends.iterrows()}
    winner_by_round = {int(r["round"]): WINNER_TEAM.get(r["winner"]) for _, r in round_ends.iterrows()}

    # Team and alive state at the start of each round, used to detect clutches.
    round_start_ticks = sorted(int(t) for t in freeze_ends["tick"])
    states = parser.parse_ticks(["team_num", "is_alive"], ticks=round_start_ticks) if round_start_ticks else None

    highlights: list[Highlight] = []
    for rnd_idx, rdeaths in deaths.groupby("total_rounds_played"):
        rnd = int(rnd_idx) + 1
        my_kills = rdeaths[(rdeaths["attacker_steamid"] == pov)
                           & (rdeaths["user_team_num"] != rdeaths["attacker_team_num"])]

        # Clutch detection: replay deaths from the round's freeze end.
        clutch_vs, clutch_tick, my_team = 0, None, None
        first_tick = int(rdeaths["tick"].iloc[0])
        start = max((t for t in round_start_ticks if t <= first_tick), default=None)
        if states is not None and start is not None:
            snap = states[states["tick"] == start]
            snap = snap.assign(steamid=snap["steamid"].astype(str))
            me = snap[snap["steamid"] == pov]
            if not me.empty:
                my_team = int(me["team_num"].iloc[0])
                alive = {str(s): int(t) for s, t in zip(snap["steamid"], snap["team_num"])
                         if t in (TEAM_T, TEAM_CT)}
                for _, d in rdeaths.iterrows():
                    alive.pop(d["user_steamid"], None)
                    if pov not in alive:
                        break
                    mates = sum(1 for t in alive.values() if t == my_team)
                    enemies = sum(1 for t in alive.values() if t != my_team)
                    if mates == 1 and enemies >= 1 and clutch_tick is None:
                        clutch_vs, clutch_tick = enemies, int(d["tick"])
                if winner_by_round.get(rnd) != my_team or clutch_vs < clutch_min_enemies:
                    clutch_vs, clutch_tick = 0, None

        if len(my_kills) < min_kills and not (clutch_vs and len(my_kills) > 0):
            continue

        kills = [Kill(int(r["tick"]), str(r["weapon"]), str(r["user_name"]), bool(r["headshot"]),
                      _flags(r)) for _, r in my_kills.iterrows()]
        n = len(kills)
        score = MULTIKILL_SCORE.get(min(n, 5), 0.0) + 2.0 * clutch_vs
        score += 0.3 * sum(k.headshot for k in kills) + 0.5 * sum(len(k.flags) for k in kills)
        span = (kills[-1].tick - kills[0].tick) / tickrate
        if n >= 3 and span < 5:
            score += 1.0  # fast multikill

        # For clutches, keep rolling until the round is decided.
        end_cap = None
        if clutch_vs and rnd in end_tick_by_round:
            rend = end_tick_by_round[rnd] + tickrate
            if rend - kills[-1].tick < 10 * tickrate:
                end_cap = rend
        seqs = _windows([k.tick for k in kills], tickrate, pre_seconds, post_seconds,
                        max_gap_seconds, end_cap)
        highlights.append(Highlight(rnd, kills, clutch_vs, round(score, 1), seqs))

    return DemoInfo(demo, header.get("map_name", "unknown"), tickrate, pov, pov_name, highlights)
