"""История матчей в памяти: матчи с пятёрками, игроки, кубки."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlayerGame:
    account_id: int
    hero_id: int
    radiant: bool
    kills: int
    deaths: int
    assists: int
    gpm: int
    xpm: int
    net_worth: int
    gold_share: float      # 1.0 = ровно десятая часть золота матча
    farm_rank: int         # 1 = больше всех золота в своей пятёрке, 5 = меньше всех


@dataclass
class Match:
    match_id: int
    league_id: int
    start_time: int
    duration: int
    tournament_id: int | None
    radiant_win: bool | None
    radiant_team_id: int | None
    dire_team_id: int | None
    radiant: tuple[PlayerGame, ...]
    dire: tuple[PlayerGame, ...]
    radiant_bans: tuple[int, ...] = ()
    dire_bans: tuple[int, ...] = ()
    # Золото и убийства OpenDota дозаполняет позже: пока их нет, матч идёт в результаты,
    # форму и Эло, но не в долю золота и место по фарму.
    has_stats: bool = True

    def side(self, radiant: bool) -> tuple[PlayerGame, ...]:
        return self.radiant if radiant else self.dire

    def team_id(self, radiant: bool) -> int | None:
        return self.radiant_team_id if radiant else self.dire_team_id

    def lineup(self, radiant: bool) -> frozenset[int]:
        return frozenset(p.account_id for p in self.side(radiant))

    @property
    def complete(self) -> bool:
        return self.radiant_win is not None and len(self.radiant) == 5 and len(self.dire) == 5


@dataclass
class PlayerInfo:
    account_id: int
    name: str
    mmr: float | None
    roles: tuple[str, ...]
    team_id: int | None
    roster_confirmed: bool


@dataclass
class Cup:
    tournament_id: int
    matches: list[Match] = field(default_factory=list)

    @property
    def start(self) -> int:
        return min(m.start_time for m in self.matches)

    @property
    def end(self) -> int:
        return max(m.start_time for m in self.matches)


@dataclass
class Dataset:
    matches: list[Match]                   # по времени начала
    players: dict[int, PlayerInfo]
    cups: dict[int, Cup]

    def cup_matches(self, tournament_id: int) -> list[Match]:
        cup = self.cups.get(tournament_id)
        return cup.matches if cup else []


def _player_games(rows: list[sqlite3.Row]) -> tuple[tuple[PlayerGame, ...], tuple[PlayerGame, ...]]:
    total_gpm = sum((r["gold_per_min"] or 0) for r in rows) or 1
    sides: dict[bool, list[sqlite3.Row]] = {True: [], False: []}
    for r in rows:
        sides[bool(r["is_radiant"])].append(r)
    out: dict[bool, tuple[PlayerGame, ...]] = {}
    for radiant, srows in sides.items():
        ordered = sorted(srows, key=lambda r: -(r["gold_per_min"] or 0))
        rank = {r["account_id"]: i + 1 for i, r in enumerate(ordered)}
        out[radiant] = tuple(PlayerGame(
            account_id=r["account_id"], hero_id=r["hero_id"] or 0, radiant=radiant,
            kills=r["kills"] or 0, deaths=r["deaths"] or 0, assists=r["assists"] or 0,
            gpm=r["gold_per_min"] or 0, xpm=r["xp_per_min"] or 0, net_worth=r["net_worth"] or 0,
            gold_share=(r["gold_per_min"] or 0) / total_gpm * 10, farm_rank=rank[r["account_id"]],
        ) for r in srows)
    return out[True], out[False]


def load_dataset(conn: sqlite3.Connection) -> Dataset:
    by_match: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM match_players"):
        by_match[r["match_id"]].append(r)
    bans: dict[int, dict[int | None, list[int]]] = defaultdict(lambda: defaultdict(list))
    for r in conn.execute("SELECT match_id, hero_id, team_id FROM match_drafts WHERE is_pick = 0"):
        bans[r["match_id"]][r["team_id"]].append(r["hero_id"])

    matches: list[Match] = []
    for r in conn.execute("SELECT * FROM matches ORDER BY start_time, match_id"):
        rows = by_match.get(r["match_id"], [])
        radiant, dire = _player_games(rows)
        b = bans.get(r["match_id"], {})
        matches.append(Match(
            match_id=r["match_id"], league_id=r["league_id"], start_time=r["start_time"],
            duration=r["duration"] or 0, tournament_id=r["tournament_id"],
            radiant_win=None if r["radiant_win"] is None else bool(r["radiant_win"]),
            radiant_team_id=r["radiant_team_id"], dire_team_id=r["dire_team_id"],
            radiant=radiant, dire=dire,
            radiant_bans=tuple(b.get(r["radiant_team_id"], ())),
            dire_bans=tuple(b.get(r["dire_team_id"], ())),
            has_stats=any(x["gold_per_min"] for x in rows),
        ))

    players = {
        r["account_id"]: PlayerInfo(
            account_id=r["account_id"], name=r["name"] or str(r["account_id"]), mmr=r["mmr"],
            roles=tuple(x for x in (r["preferred_roles"] or "").split(",") if x),
            team_id=r["team_id"], roster_confirmed=bool(r["roster_confirmed"]))
        for r in conn.execute("SELECT * FROM players")}

    cups: dict[int, Cup] = {}
    for m in matches:
        if m.tournament_id is not None and m.complete:
            cups.setdefault(m.tournament_id, Cup(m.tournament_id)).matches.append(m)
    return Dataset(matches=matches, players=players, cups=cups)
