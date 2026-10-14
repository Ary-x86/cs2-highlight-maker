"""Find highlight moments for the local player in a CS2 demo.

Two kinds of highlights:
- rounds: 3k / 4k / ace (the whole enemy team, so 2 in Wingman and 3 in Rush) and won clutches, clipped whole;
- special kills: single kills worth showing on their own (flicks, spray transfers, wallbangs, ...), see SPECIAL_RULES.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from demoparser2 import DemoParser

TEAM_T, TEAM_CT = 2, 3
WINNER_TEAM = {"T": TEAM_T, "CT": TEAM_CT, 2: TEAM_T, 3: TEAM_CT}
MULTIKILL_SCORE = {3: 3.0, 4: 5.0, 5: 8.0}
ACE_SCORE = 8.0

SNIPERS = {"awp", "ssg08"}
AUTOMATICS = {"ak47", "m4a1", "m4a1_silencer", "m4a4", "galilar", "famas", "aug", "sg556", "mac10", "mp9", "mp7",
              "mp5sd", "ump45", "p90", "bizon", "negev", "m249", "cz75a"}
NOT_AIMED = {"hegrenade", "inferno", "molotov", "incgrenade", "flashbang", "smokegrenade", "decoy", "world",
             "planted_c4", "c4"}

FLICK_WINDOW = 0.2          # seconds before the first hit that the crosshair travel is measured over
FLICK_DEGREES = 30.0        # rifles, pistols, ...
SNIPER_FLICK_DEGREES = 20.0
LONG_RANGE_METERS = 40.0
SPRAY_TRANSFER_SECONDS = 1.5
SPRAY_MAX_PAUSE = 0.3       # longer gap between shots ends the spray
HIT_CHAIN_GAP = 0.5         # hits closer than this belong to the same fight

# Special kill types, the label used in clip names and the score they add.
SPECIAL_RULES = {
    "flick": ("flick", 1.5),
    "sniper_flick": ("snipeflick", 1.5),
    "spray_transfer": ("spraytransfer", 1.5),
    "long_range": ("longshot", 1.0),
    "noscope": ("noscope", 2.0),
    "wallbang": ("wallbang", 1.0),
    "smoke": ("smoke", 1.0),
    "jumpshot": ("jumpshot", 1.0),
    "blind": ("blind", 1.5),
}


class NotYourMatch(ValueError):
    """None of the given accounts played in the demo."""


@dataclass
class Kill:
    tick: int
    weapon: str
    victim: str
    headshot: bool
    flags: list[str]
    victim_steamid: str = ""


@dataclass
class Highlight:
    round: int
    kills: list[Kill]
    clutch_vs: int = 0  # 1vN won, 0 if not a clutch
    ace: bool = False
    whole_round: bool = True  # False: only selected special kills from this round
    tags: list[str] = field(default_factory=list)  # special kill types shown in the name
    score: float = 0.0
    player_slot: int = 0  # spectator slot of the POV player (user_id + 1)
    # (start_tick, end_tick) windows; several when kills are far apart so dead time gets cut
    sequences: list[tuple[int, int]] = field(default_factory=list)

    @property
    def label(self) -> str:
        parts = []
        if self.clutch_vs:
            parts.append(f"1v{self.clutch_vs}")
        n = len(self.kills)
        if self.whole_round and (n >= 2 or not self.clutch_vs):
            parts.append("ace" if self.ace else f"{n}k")
        parts += [SPECIAL_RULES[t][0] for t in self.tags[:2]]
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
        flags.append("jumpshot")
    if row.get("attackerblind"):
        flags.append("blind")
    if (row.get("distance") or 0) >= LONG_RANGE_METERS:
        flags.append("long_range")
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


def _angle(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Degrees between two (pitch, yaw) view directions."""
    dyaw = (b[1] - a[1] + 180) % 360 - 180
    return math.hypot(dyaw, b[0] - a[0])


def _event(parser: DemoParser, name: str, steamid: str):
    """Rows of one event fired by steamid (as attacker for player_hurt, as user for weapon_fire)."""
    try:
        df = parser.parse_event(name)
    except Exception:  # noqa: BLE001 - some demos lack an event entirely
        return []
    col = "attacker_steamid" if name == "player_hurt" else "user_steamid"
    if df is None or df.empty or col not in df:
        return []
    df = df[df[col].astype(str) == steamid]
    return [r for _, r in df.sort_values("tick").iterrows()]


def tag_special_kills(parser: DemoParser, pov: str, kills: list[Kill], wanted: set[str], tickrate: int) -> None:
    """Add flick / sniper_flick / spray_transfer to Kill.flags (the others come straight from player_death)."""
    if not kills or not wanted & {"flick", "sniper_flick", "spray_transfer"}:
        return

    if "spray_transfer" in wanted:
        fires = [int(r["tick"]) for r in _event(parser, "weapon_fire", pov)]
        ordered = sorted(kills, key=lambda k: k.tick)
        for a, b in zip(ordered, ordered[1:]):
            if a.weapon != b.weapon or a.weapon not in AUTOMATICS or a.victim_steamid == b.victim_steamid:
                continue
            if b.tick - a.tick > SPRAY_TRANSFER_SECONDS * tickrate:
                continue
            shots = [t for t in fires if a.tick - tickrate <= t <= b.tick]
            marks = sorted({a.tick, *shots, b.tick})
            if shots and all(y - x <= SPRAY_MAX_PAUSE * tickrate for x, y in zip(marks, marks[1:])):
                for k in (a, b):
                    if "spray_transfer" not in k.flags:
                        k.flags.append("spray_transfer")

    if not wanted & {"flick", "sniper_flick"}:
        return
    # First hit of the fight that ended in each kill: the moment the flick has to land.
    hits: dict[str, list[int]] = {}
    for r in _event(parser, "player_hurt", pov):
        hits.setdefault(str(r["user_steamid"]), []).append(int(r["tick"]))
    first_hit = {}
    for k in kills:
        chain = [t for t in hits.get(k.victim_steamid, []) if t <= k.tick]
        start = k.tick
        for t in reversed(chain):
            if start - t > HIT_CHAIN_GAP * tickrate:
                break
            start = t
        first_hit[id(k)] = start
    back = max(1, round(FLICK_WINDOW * tickrate))
    ticks = sorted({t for k in kills for t in (first_hit[id(k)] - 1, first_hit[id(k)] - 1 - back)})
    df = parser.parse_ticks(["pitch", "yaw"], ticks=ticks)
    df = df[df["steamid"].astype(str) == pov]
    view = {int(t): (float(p), float(y)) for t, p, y in zip(df["tick"], df["pitch"], df["yaw"])}

    for k in kills:
        if k.weapon in NOT_AIMED or k.weapon.startswith("knife") or k.weapon == "bayonet":
            continue
        shot = first_hit[id(k)] - 1
        a, b = view.get(shot - back), view.get(shot)
        if a and b:
            turned = _angle(a, b)
            if k.weapon in SNIPERS:
                if turned >= SNIPER_FLICK_DEGREES:
                    k.flags.append("sniper_flick")
            elif turned >= FLICK_DEGREES:
                k.flags.append("flick")


def analyze(demo: Path, steamids: list[str], *, round_types: list[str] | tuple = ("3k", "4k", "ace", "clutch"),
            special_kills: list[str] | tuple = (), clutch_min_enemies: int = 2,
            pre_seconds: float = 3.0, post_seconds: float = 2.0, max_gap_seconds: float = 8.0) -> DemoInfo:
    parser = DemoParser(str(demo))
    header = parser.parse_header()
    round_types, specials = set(round_types), set(special_kills)

    players = parser.parse_player_info()
    present = set(players["steamid"].astype(str))
    pov = next((s for s in steamids if s in present), None)
    if pov is None:
        raise NotYourMatch(f"none of {steamids} played in {demo.name}")
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

    # Team and alive state at the start of each round, used for clutches and team sizes.
    round_start_ticks = sorted(int(t) for t in freeze_ends["tick"])
    states = parser.parse_ticks(["team_num", "is_alive"], ticks=round_start_ticks) if round_start_ticks else None

    rounds = []  # (round number, my kills, clutch_vs, enemy count)
    for rnd_idx, rdeaths in deaths.groupby("total_rounds_played"):
        rnd = int(rnd_idx) + 1
        my_kills = rdeaths[(rdeaths["attacker_steamid"] == pov)
                           & (rdeaths["user_team_num"] != rdeaths["attacker_team_num"])]
        if my_kills.empty:
            continue

        # Clutch detection: replay deaths from the round's freeze end.
        clutch_vs, enemies_at_start = 0, 5
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
                enemies_at_start = sum(1 for t in alive.values() if t != my_team) or 5
                for _, d in rdeaths.iterrows():
                    alive.pop(d["user_steamid"], None)
                    if pov not in alive:
                        break
                    mates = sum(1 for t in alive.values() if t == my_team)
                    enemies = sum(1 for t in alive.values() if t != my_team)
                    if mates == 1 and enemies >= 1 and not clutch_vs:
                        clutch_vs = enemies
                if winner_by_round.get(rnd) != my_team or clutch_vs < clutch_min_enemies:
                    clutch_vs = 0

        kills = [Kill(int(r["tick"]), str(r["weapon"]), str(r["user_name"]), bool(r["headshot"]),
                      _flags(r), str(r["user_steamid"])) for _, r in my_kills.iterrows()]
        rounds.append((rnd, kills, clutch_vs, enemies_at_start))

    if specials:
        tag_special_kills(parser, pov, [k for _, ks, _, _ in rounds for k in ks], specials, tickrate)

    highlights: list[Highlight] = []
    for rnd, kills, clutch_vs, enemies in rounds:
        n = len(kills)
        ace = n >= max(2, enemies) or n >= 5
        kind = "ace" if ace else {3: "3k", 4: "4k"}.get(n)
        whole = kind in round_types or bool(clutch_vs and "clutch" in round_types)
        if not whole:
            kills = [k for k in kills if specials & set(k.flags)]
            if not kills:
                continue
            clutch_vs = 0
        tags = [t for t in SPECIAL_RULES if t in specials and any(t in k.flags for k in kills)]

        score = (ACE_SCORE if ace else MULTIKILL_SCORE.get(n, 0.0)) if whole else 0.0
        score += 2.0 * clutch_vs + 0.3 * sum(k.headshot for k in kills)
        score += sum(SPECIAL_RULES[f][1] for k in kills for f in k.flags if f in SPECIAL_RULES)
        span = (kills[-1].tick - kills[0].tick) / tickrate
        if n >= 3 and whole and span < 5:
            score += 1.0  # fast multikill

        # For clutches, keep rolling until the round is decided.
        end_cap = None
        if clutch_vs and rnd in end_tick_by_round:
            rend = end_tick_by_round[rnd] + tickrate
            if rend - kills[-1].tick < 10 * tickrate:
                end_cap = rend
        seqs = _windows([k.tick for k in kills], tickrate, pre_seconds, post_seconds, max_gap_seconds, end_cap)
        highlights.append(Highlight(rnd, kills, clutch_vs=clutch_vs, ace=ace and whole, whole_round=whole,
                                    tags=tags, score=round(score, 1), sequences=seqs))

    # user_id can change after a reconnect, so look it up at each highlight's start.
    if highlights:
        starts = [h.sequences[0][0] for h in highlights]
        ids = parser.parse_ticks(["user_id"], ticks=starts)
        ids = ids[ids["steamid"].astype(str) == pov]
        slot_at = {int(t): int(u) + 1 for t, u in zip(ids["tick"], ids["user_id"])}
        fallback = next(iter(slot_at.values()), 0)
        for h, start in zip(highlights, starts):
            h.player_slot = slot_at.get(start, fallback)

    return DemoInfo(demo, header.get("map_name", "unknown"), tickrate, pov, pov_name, highlights)
