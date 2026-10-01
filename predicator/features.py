"""Признаки игроков, составов и драфта.

Всё считается по снимку истории `History`: агрегаты по матчам, сыгранным до
заданного момента. Для проверки снимок берётся на начало кубка (признаки строго
по прошлым играм), для живого прогноза — по всей истории.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .dataset import Dataset, Match

ELO_START = 1500.0
ELO_K = 24.0


@dataclass
class PlayerStat:
    games: int = 0
    wins: int = 0
    stat_games: int = 0            # игры, где известно золото (знаменатель для gold и rank)
    gold_sum: float = 0.0
    rank_sum: float = 0.0
    elo: float = ELO_START
    elo_games: int = 0
    stints: dict[tuple[int, int], int] = field(default_factory=dict)
    hero_games: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    hero_wins: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    hero_gold: dict[int, float] = field(default_factory=lambda: defaultdict(float))


@dataclass
class HeroStat:
    games: int = 0
    wins: int = 0
    stat_games: int = 0
    rank_sum: float = 0.0
    gold_sum: float = 0.0


class History:
    """Агрегаты по матчам в хронологическом порядке."""

    def __init__(self) -> None:
        self.players: dict[int, PlayerStat] = defaultdict(PlayerStat)
        self.heroes: dict[int, HeroStat] = defaultdict(HeroStat)
        self.team_games: dict[tuple[int, int], int] = defaultdict(int)
        self.ally: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0])
        self.versus: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0])
        self.matches = 0
        self.last_time = 0

    @classmethod
    def build(cls, matches: Iterable[Match]) -> "History":
        h = cls()
        for m in matches:
            h.add(m)
        return h

    @classmethod
    def before(cls, ds: Dataset, cutoff: int) -> "History":
        return cls.build(m for m in ds.matches if m.start_time < cutoff)

    def add(self, m: Match) -> None:
        if len(m.radiant) != 5 or len(m.dire) != 5:
            return
        self.matches += 1
        self.last_time = max(self.last_time, m.start_time)
        result = m.radiant_win
        for radiant in (True, False):
            side = m.side(radiant)
            won = None if result is None else (result == radiant)
            team = m.team_id(radiant)
            if m.tournament_id is not None and team is not None:
                self.team_games[(m.tournament_id, team)] += 1
            for p in side:
                st = self.players[p.account_id]
                st.games += 1
                st.hero_games[p.hero_id] += 1
                if m.tournament_id is not None and team is not None:
                    key = (m.tournament_id, team)
                    st.stints[key] = st.stints.get(key, 0) + 1
                hs = self.heroes[p.hero_id]
                hs.games += 1
                if m.has_stats:
                    st.stat_games += 1
                    st.gold_sum += p.gold_share
                    st.rank_sum += p.farm_rank
                    st.hero_gold[p.hero_id] += p.gold_share
                    hs.stat_games += 1
                    hs.rank_sum += p.farm_rank
                    hs.gold_sum += p.gold_share
                if won:
                    st.wins += 1
                    st.hero_wins[p.hero_id] += 1
                    hs.wins += 1
            if won is not None:
                heroes = sorted(p.hero_id for p in side)
                for i in range(5):
                    for j in range(i + 1, 5):
                        rec = self.ally[(heroes[i], heroes[j])]
                        rec[0] += 1
                        rec[1] += int(won)
                for a in side:
                    for b in m.side(not radiant):
                        rec = self.versus[(a.hero_id, b.hero_id)]
                        rec[0] += 1
                        rec[1] += int(won)
        if result is not None:
            self._update_elo(m, result)

    def _update_elo(self, m: Match, radiant_win: bool) -> None:
        r = [self.players[p.account_id] for p in m.radiant]
        d = [self.players[p.account_id] for p in m.dire]
        ra = sum(s.elo for s in r) / 5
        da = sum(s.elo for s in d) / 5
        expected = 1.0 / (1.0 + 10 ** ((da - ra) / 400.0))
        delta = ELO_K * ((1.0 if radiant_win else 0.0) - expected)
        for s in r:
            s.elo += delta
            s.elo_games += 1
        for s in d:
            s.elo -= delta
            s.elo_games += 1

    # --- признаки игрока -------------------------------------------------

    def durability(self, account_id: int) -> float | None:
        st = self.players.get(account_id)
        if not st or not st.stints:
            return None
        shares = [n / self.team_games[key] for key, n in st.stints.items() if self.team_games[key]]
        return sum(shares) / len(shares) if shares else None

    def gold(self, account_id: int, shrink: float = 0.0, prior: float = 1.0) -> float | None:
        st = self.players.get(account_id)
        if not st or not st.stat_games:
            return None
        return (st.gold_sum + shrink * prior) / (st.stat_games + shrink)

    def farm_rank(self, account_id: int) -> float | None:
        st = self.players.get(account_id)
        if not st or not st.stat_games:
            return None
        return st.rank_sum / st.stat_games

    def elo(self, account_id: int) -> float:
        st = self.players.get(account_id)
        return st.elo if st else ELO_START


# --- состав ---------------------------------------------------------------

ROSTER_FEATURES = ("gold", "elo_max", "durability", "mmr")


@dataclass
class Imputation:
    """Чем заполнять признаки игроков без истории."""

    gold: float = 0.95
    durability: float = 0.6
    mmr: float = 6300.0


def roster_features(hist: History, lineup: Sequence[int], mmr: dict[int, float | None],
                    imp: Imputation, gold_shrink: float = 0.0) -> dict[str, float]:
    golds, durs, elos, mmrs = [], [], [], []
    for acc in lineup:
        g = hist.gold(acc, gold_shrink, imp.gold)
        golds.append(imp.gold if g is None else g)
        d = hist.durability(acc)
        durs.append(imp.durability if d is None else d)
        elos.append(hist.elo(acc))
        v = mmr.get(acc)
        mmrs.append(imp.mmr if v is None else v)
    n = max(len(lineup), 1)
    return {
        "gold": sum(golds) / n,
        "elo_max": max(elos) if elos else ELO_START,
        "durability": sum(durs) / n,
        "mmr": sum(mmrs) * 5 / n,
    }


def default_imputation(hist: History, mmr: dict[int, float | None]) -> Imputation:
    """Новичка считаем игроком чуть ниже среднего: медианы по истории."""
    golds = sorted(st.gold_sum / st.stat_games for st in hist.players.values() if st.stat_games >= 5)
    durs = sorted(d for d in (hist.durability(a) for a in hist.players) if d is not None)
    mmrs = sorted(v for v in mmr.values() if v)

    def q(xs: list[float], p: float, default: float) -> float:
        return xs[int(p * (len(xs) - 1))] if xs else default

    return Imputation(gold=q(golds, 0.4, 0.95), durability=q(durs, 0.5, 0.6),
                      mmr=q(mmrs, 0.4, 6300.0))


def credited_form(previous: Iterable[Match], lineup: Iterable[int],
                  power: float = 3.0) -> tuple[float, float]:
    """Зачтённые победы и игры состава в идущем кубке.

    Прошлая игра засчитывается пропорционально тому, сколько человек из нынешней
    пятёрки играли её на одной стороне: вес = (совпало / 5) ** power.
    """
    lineup = frozenset(lineup)
    wins = games = 0.0
    for m in previous:
        if m.radiant_win is None:
            continue
        for radiant in (True, False):
            overlap = len(lineup & m.lineup(radiant))
            if overlap == 0:
                continue
            w = (overlap / 5) ** power
            games += w
            if m.radiant_win == radiant:
                wins += w
    return wins, games


def logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)
