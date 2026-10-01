"""Модель исхода игры.

Три слоя, каждый проверен протоколом «обучение без одного кубка» (см. backtest.py):

1. Оценка состава (как в заметке): четыре признака пятёрки по играм ДО кубка —
   доля золота, Эло сильнейшего, доигрываемость, суммарный MMR. Нормируются
   внутри кубка, вес — гребневая регрессия (λ=60) по командам прошлых кубков.
   Рейтинг состава в логитах = scale × (0.5 + Σ вес × z).
2. Форма в идущем кубке: после каждой игры рейтинг составов сдвигается на
   k × (результат − ожидание), то есть победа над сильным весит больше победы
   над слабым. Игра засчитывается составу с весом (совпало_человек / 5) ** 2,
   поэтому перебранная команда возвращается к оценке по составу.
3. Драфт (если герои известны): винрейт героя в лиге и опыт игрока на герое.
   Логистическая регрессия поверх логита п.1–2.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .dataset import Cup, Dataset, Match
from .features import (ROSTER_FEATURES, History, Imputation, default_imputation, logit,
                       roster_features, sigmoid)
from .linalg import logistic, mean_std, ridge

DRAFT_FEATURES = ("hero_wr", "comfort")
MIN_GAMES_FOR_COMFORT = 10


@dataclass(frozen=True)
class Params:
    ridge_lambda: float = 60.0
    scale: float = 6.0
    gold_shrink: float = 3.0
    form_k: float = 0.35
    form_power: float = 2.0
    hero_k: float = 30.0
    draft_l2: float = 1.0


# --- слой 1: оценка состава ------------------------------------------------

@dataclass
class Norm:
    """Среднее и разброс признаков внутри кубка."""

    stats: dict[str, tuple[float, float]]

    @classmethod
    def fit(cls, rows: Sequence[dict[str, float]]) -> "Norm":
        return cls({k: mean_std([r[k] for r in rows]) for k in ROSTER_FEATURES})

    def z(self, feats: dict[str, float]) -> list[float]:
        return [(feats[k] - self.stats[k][0]) / self.stats[k][1] for k in ROSTER_FEATURES]


@dataclass
class CupData:
    """Всё про один кубок для обучения и проверки: признаки строго до его начала."""

    cup: Cup
    hist: History
    imp: Imputation
    match_feats: list[dict[bool, dict[str, float]]]
    teams: dict[int | None, tuple[dict[str, float], float, int]]   # признаки, доля побед, игр
    norm: Norm


def prepare_cup(ds: Dataset, cup: Cup, params: Params, mmr: dict[int, float | None]) -> CupData:
    hist = History.before(ds, cup.start)
    imp = default_imputation(hist, mmr)
    team_rows: dict[int | None, list[dict[str, float]]] = {}
    team_res: dict[int | None, list[int]] = {}
    match_feats = []
    for m in cup.matches:
        fs = {}
        for radiant in (True, False):
            f = roster_features(hist, [p.account_id for p in m.side(radiant)], mmr, imp,
                                params.gold_shrink)
            fs[radiant] = f
            key = m.team_id(radiant)
            team_rows.setdefault(key, []).append(f)
            res = team_res.setdefault(key, [0, 0])
            res[0] += int(m.radiant_win == radiant)
            res[1] += 1
        match_feats.append(fs)
    teams = {key: ({k: sum(r[k] for r in rows) / len(rows) for k in ROSTER_FEATURES},
                   team_res[key][0] / team_res[key][1], team_res[key][1])
             for key, rows in team_rows.items()}
    norm = Norm.fit([t[0] for t in teams.values()])
    return CupData(cup, hist, imp, match_feats, teams, norm)


def fit_roster_weights(cups: Iterable[CupData], params: Params) -> list[float]:
    x, y = [], []
    for c in cups:
        for feats, share, _ in c.teams.values():
            x.append(c.norm.z(feats))
            y.append(share - 0.5)
    return ridge(x, y, params.ridge_lambda)


def roster_score(weights: Sequence[float], norm: Norm, feats: dict[str, float]) -> float:
    return 0.5 + sum(w * z for w, z in zip(weights, norm.z(feats)))


# --- слой 2: форма в кубке ---------------------------------------------------

@dataclass
class CupRater:
    """Сыгранные игры кубка и поправки к рейтингу составов по их результатам."""

    params: Params
    games: list[tuple[frozenset[int], frozenset[int], float, float]] = field(default_factory=list)

    def form(self, lineup: Iterable[int]) -> tuple[float, float, float]:
        """(поправка в логитах, зачтённые игры, зачтённые победы)."""
        lineup = frozenset(lineup)
        adj = games = wins = 0.0
        for radiant, dire, y, p in self.games:
            for side, yy, pp in ((radiant, y, p), (dire, 1 - y, 1 - p)):
                overlap = len(lineup & side)
                if overlap:
                    w = (overlap / 5) ** self.params.form_power
                    adj += w * self.params.form_k * (yy - pp)
                    games += w
                    wins += w * yy
        return adj, games, wins

    def add(self, radiant: frozenset[int], dire: frozenset[int], radiant_win: bool,
            p_radiant: float) -> None:
        self.games.append((radiant, dire, 1.0 if radiant_win else 0.0, p_radiant))


def replay_cup(c: CupData, weights: Sequence[float], params: Params) -> list[float]:
    """Прогнать кубок по порядку: логит до драфта для каждой игры (форма только по прошлым)."""
    rater = CupRater(params)
    out = []
    for m, fs in zip(c.cup.matches, c.match_feats):
        rating = {}
        for radiant in (True, False):
            adj, _, _ = rater.form(m.lineup(radiant))
            rating[radiant] = params.scale * roster_score(weights, c.norm, fs[radiant]) + adj
        z = rating[True] - rating[False]
        out.append(z)
        rater.add(m.lineup(True), m.lineup(False), bool(m.radiant_win), sigmoid(z))
    return out


# --- слой 3: драфт -----------------------------------------------------------

def hero_strength(hist: History, hero_id: int, params: Params) -> float:
    hs = hist.heroes.get(hero_id)
    games, wins = (hs.games, hs.wins) if hs else (0, 0)
    return logit((wins + params.hero_k * 0.5) / (games + params.hero_k))


def mean_comfort(hist: History) -> float:
    """Средний log(1 + игр игрока на герое) по всем пикам истории: «обычный» пик."""
    total = n = 0.0
    for st in hist.players.values():
        for games in st.hero_games.values():
            # каждая из games игр была пиком с опытом от 0 до games-1
            total += sum(math.log1p(i) for i in range(games))
            n += games
    return total / n if n else 0.0


def comfort(hist: History, account_id: int, hero_id: int, center: float) -> float:
    st = hist.players.get(account_id)
    if not st or st.games < MIN_GAMES_FOR_COMFORT:
        return 0.0
    return math.log1p(st.hero_games.get(hero_id, 0)) - center


def side_draft(hist: History, picks: Sequence[tuple[int | None, int | None]], params: Params,
               center: float) -> dict[str, float]:
    """picks: (account_id, hero_id); неизвестные герои дают ноль (средний пик)."""
    out = {"hero_wr": 0.0, "comfort": 0.0}
    for acc, hero in picks:
        if hero is None:
            continue
        out["hero_wr"] += hero_strength(hist, hero, params)
        if acc is not None:
            out["comfort"] += comfort(hist, acc, hero, center)
    return out


def draft_delta(hist: History, a: Sequence[tuple[int | None, int | None]],
                b: Sequence[tuple[int | None, int | None]], params: Params,
                center: float) -> list[float]:
    fa, fb = side_draft(hist, a, params, center), side_draft(hist, b, params, center)
    return [fa[k] - fb[k] for k in DRAFT_FEATURES]


# --- обучение целиком --------------------------------------------------------

@dataclass
class TrainedModel:
    params: Params
    weights: list[float]                 # оценка состава, по ROSTER_FEATURES
    draft_coef: list[float]              # [логит до драфта, *DRAFT_FEATURES]
    hist: History                        # вся история (для драфта)
    comfort_center: float
    cups_used: list[int]
    n_matches: int

    def combine(self, pre_logit: float, draft: Sequence[float] | None) -> float:
        if draft is None:
            return pre_logit
        return self.draft_coef[0] * pre_logit + sum(
            c * d for c, d in zip(self.draft_coef[1:], draft))


def match_picks(m: Match, radiant: bool) -> list[tuple[int | None, int | None]]:
    return [(p.account_id, p.hero_id) for p in m.side(radiant)]


def training_rows(ds: Dataset, cups: dict[int, CupData], params: Params,
                  loco_weights: dict[int, list[float]]) -> dict[int, tuple[int, float, list[float], float]]:
    """По каждой игре кубков: (кубок, логит до драфта вне выборки, драфт, исход).

    Признаки драфта считаются по всем играм строго раньше этой (история растёт по ходу).
    """
    pre: dict[int, tuple[int, float]] = {}
    for t, c in cups.items():
        for m, z in zip(c.cup.matches, replay_cup(c, loco_weights[t], params)):
            pre[m.match_id] = (t, z)
    rows = {}
    hist = History()
    center_sum = center_n = 0.0
    for m in ds.matches:
        if m.match_id in pre:
            center = center_sum / center_n if center_n else 0.0
            d = draft_delta(hist, match_picks(m, True), match_picks(m, False), params, center)
            t, z = pre[m.match_id]
            rows[m.match_id] = (t, z, d, 1.0 if m.radiant_win else 0.0)
        if len(m.radiant) == 5 and len(m.dire) == 5:
            for p in m.radiant + m.dire:
                st = hist.players.get(p.account_id)
                center_sum += math.log1p(st.hero_games.get(p.hero_id, 0) if st else 0)
                center_n += 1
        hist.add(m)
    return rows


def finished_cups(ds: Dataset, now: int, exclude: Iterable[int] = (), min_matches: int = 100,
                  quiet_hours: float = 24) -> list[Cup]:
    """Завершённые кубки: достаточно игр и давно нет новых."""
    skip = set(exclude)
    return [c for t, c in sorted(ds.cups.items(), key=lambda kv: kv[1].start)
            if t not in skip and len(c.matches) >= min_matches
            and now - c.end > quiet_hours * 3600]


def train(ds: Dataset, now: int, params: Params = Params(),
          exclude: Iterable[int] = ()) -> TrainedModel:
    if not ds.matches:
        raise RuntimeError("истории матчей ещё нет: идёт первая загрузка бэкапа")
    mmr = {a: p.mmr for a, p in ds.players.items()}
    cups = {c.tournament_id: prepare_cup(ds, c, params, mmr)
            for c in finished_cups(ds, now, exclude)}
    if len(cups) < 2:
        raise RuntimeError("для обучения нужно хотя бы два завершённых кубка")
    weights = fit_roster_weights(cups.values(), params)
    loco = {t: fit_roster_weights([c for u, c in cups.items() if u != t], params) for t in cups}
    rows = training_rows(ds, cups, params, loco)
    x = [[z, *d] for (_, z, d, _) in rows.values()]
    y = [r[3] for r in rows.values()]
    coef = logistic(x, y, l2=params.draft_l2)
    hist = History.build(ds.matches)
    return TrainedModel(params=params, weights=weights, draft_coef=coef, hist=hist,
                        comfort_center=mean_comfort(hist), cups_used=sorted(cups),
                        n_matches=len(rows))
