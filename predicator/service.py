"""Прогнозы для живых составов: склейка модели, истории и текущих команд."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Sequence

from .config import Settings
from .dataset import Dataset, load_dataset
from .db import connect, get_meta
from .features import (ROSTER_FEATURES, History, Imputation, default_imputation, roster_features,
                       sigmoid)
from .heroes import hero_by_id
from .model import (CupRater, Norm, TrainedModel, comfort, draft_delta, hero_strength,
                    roster_score, train)
from .rosters import Team, load_teams

CONFIDENCE_CAP = 0.93    # крайние прогнозы по проверке чуть самоувереннее факта


@dataclass
class CupContext:
    tournament_id: int
    teams: dict[str, Team]
    hist_pre: History                  # история до начала кубка (признаки состава)
    imp: Imputation
    norm: Norm
    rater: CupRater
    games_played: int
    mmr: dict[int, float | None]


class PredictorService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._version: str | None = None
        self._ds: Dataset | None = None
        self._model: TrainedModel | None = None
        self._model_error: str | None = None
        self._cup_cache: dict[tuple, CupContext] = {}
        self._hist_pre_cache: dict[tuple, History] = {}

    def connect(self) -> sqlite3.Connection:
        return connect(self.settings.db_path)

    # --- модель ----------------------------------------------------------

    def refresh(self, conn: sqlite3.Connection, force: bool = False) -> None:
        version = get_meta(conn, "data_version", "0")
        with self._lock:
            if not force and version == self._version and self._ds is not None:
                return
            ds = load_dataset(conn)
            try:
                model = train(ds, int(time.time()))
                error = None
            except RuntimeError as exc:
                model, error = None, str(exc)
            self._ds, self._model, self._model_error = ds, model, error
            self._version = version
            self._cup_cache.clear()
            self._hist_pre_cache.clear()

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def dataset(self) -> Dataset:
        assert self._ds is not None
        return self._ds

    @property
    def model(self) -> TrainedModel:
        if self._model is None:
            raise RuntimeError(self._model_error or "модель не обучена")
        return self._model

    def status(self) -> dict:
        m = self._model
        return {
            "ready": m is not None,
            "error": self._model_error,
            "matches": len(self._ds.matches) if self._ds else 0,
            "cups_used": m.cups_used if m else [],
            "trained_on": m.n_matches if m else 0,
            "weights": dict(zip(ROSTER_FEATURES, m.weights)) if m else {},
            "draft_coef": m.draft_coef if m else [],
            "data_version": self._version,
        }

    # --- контекст кубка --------------------------------------------------

    def _mmr(self) -> dict[int, float | None]:
        return {a: p.mmr for a, p in self.dataset.players.items()}

    def cup_context(self, conn: sqlite3.Connection, tournament_id: int) -> CupContext:
        self.refresh(conn)
        teams = {t.key: t for t in load_teams(conn, tournament_id)}
        # MMR из текущих составов свежее датасета: замены из очереди, живой mixer-cup
        roster_mmr = {p.account_id: p.mmr for t in teams.values() for p in t.players
                      if p.mmr is not None}
        signature = (self._version, tournament_id,
                     tuple(sorted((k, tuple(t.lineup)) for k, t in teams.items())),
                     tuple(sorted(roster_mmr.items())))
        with self._lock:
            cached = self._cup_cache.get(signature)
            if cached:
                return cached
            ds, model = self.dataset, self.model
            mmr = {**self._mmr(), **roster_mmr}
            cup = ds.cups.get(tournament_id)
            # кубок без сыгранных игр: признаки по всей истории
            start = cup.start if cup else (ds.matches[-1].start_time + 1 if ds.matches else 0)
            hist_key = (self._version, start)
            hist_pre = self._hist_pre_cache.get(hist_key)
            if hist_pre is None:
                hist_pre = self._hist_pre_cache[hist_key] = History.before(ds, start)
            imp = default_imputation(hist_pre, mmr)
            feats = {k: roster_features(hist_pre, t.lineup[:5], mmr, imp, model.params.gold_shrink)
                     for k, t in teams.items() if len(t.lineup) >= 5}
            norm = Norm.fit(list(feats.values())) if len(feats) >= 2 else Norm(
                {k: (0.0, 1.0) for k in ROSTER_FEATURES})
            rater = CupRater(model.params)
            played = 0
            for m in (cup.matches if cup else []):
                rating = {}
                for radiant in (True, False):
                    f = roster_features(hist_pre, [p.account_id for p in m.side(radiant)], mmr,
                                        imp, model.params.gold_shrink)
                    adj, _, _ = rater.form(m.lineup(radiant))
                    rating[radiant] = model.params.scale * roster_score(model.weights, norm, f) + adj
                rater.add(m.lineup(True), m.lineup(False), bool(m.radiant_win),
                          sigmoid(rating[True] - rating[False]))
                played += 1
            ctx = CupContext(tournament_id, teams, hist_pre, imp, norm, rater, played, mmr)
            self._cup_cache[signature] = ctx
            return ctx

    # --- прогноз ---------------------------------------------------------

    def player_stats(self, account_id: int, hist: History) -> dict:
        st = hist.players.get(account_id)
        if not st or not st.games:
            return {"games": 0, "winrate": None, "gold": None, "elo": round(hist.elo(account_id)),
                    "durability": None, "farm_rank": None}
        return {
            "games": st.games, "winrate": round(st.wins / st.games, 3),
            "gold": round(st.gold_sum / st.games, 3), "elo": round(st.elo),
            "durability": None if hist.durability(account_id) is None
            else round(hist.durability(account_id), 3),
            "farm_rank": round(st.rank_sum / st.games, 2),
        }

    def top_heroes(self, account_id: int, n: int = 8) -> list[dict]:
        st = self.model.hist.players.get(account_id)
        if not st:
            return []
        items = sorted(st.hero_games.items(), key=lambda kv: -kv[1])[:n]
        return [{"hero_id": h, "games": g, "wins": st.hero_wins.get(h, 0)} for h, g in items]

    def team_rating(self, ctx: CupContext, lineup: Sequence[int]) -> dict:
        model = self.model
        feats = roster_features(ctx.hist_pre, lineup, ctx.mmr, ctx.imp, model.params.gold_shrink)
        z = ctx.norm.z(feats)
        score = roster_score(model.weights, ctx.norm, feats)
        adj, games, wins = ctx.rater.form(lineup)
        return {
            "features": {k: round(v, 4) for k, v in feats.items()},
            "z": {k: round(v, 3) for k, v in zip(ROSTER_FEATURES, z)},
            "contrib": {k: round(model.params.scale * w * v, 4)
                        for k, w, v in zip(ROSTER_FEATURES, model.weights, z)},
            "score": round(score, 4),
            "form": {"adj": round(adj, 4), "games": round(games, 2), "wins": round(wins, 2)},
            "rating": model.params.scale * score + adj,
        }

    def predict(self, conn: sqlite3.Connection, tournament_id: int, team_a: str, team_b: str,
                lineup_a: Sequence[int] | None = None, lineup_b: Sequence[int] | None = None,
                heroes_a: Sequence[int | None] | None = None,
                heroes_b: Sequence[int | None] | None = None) -> dict:
        ctx = self.cup_context(conn, tournament_id)
        model = self.model
        out: dict = {"tournament_id": tournament_id, "games_played": ctx.games_played}
        sides = {}
        for side, key, lineup, heroes in (("a", team_a, lineup_a, heroes_a),
                                          ("b", team_b, lineup_b, heroes_b)):
            team = ctx.teams.get(key)
            if team is None:
                raise ValueError(f"команда {key} не найдена в турнире {tournament_id}")
            lineup = list(lineup or team.lineup[:5])
            if len(lineup) != 5 or len(set(lineup)) != 5:
                raise ValueError(f"в составе {team.name} должно быть 5 разных игроков")
            heroes = list(heroes or [None] * 5) + [None] * (5 - len(heroes or []))
            names = {p.account_id: p for p in team.players}
            players = []
            for acc, hero in zip(lineup, heroes[:5]):
                info = self.dataset.players.get(acc)
                rp = names.get(acc)
                p = {
                    "account_id": acc,
                    "name": (rp.name if rp else info.name if info else str(acc)),
                    "mmr": (rp.mmr if rp else info.mmr if info else None),
                    "hero_id": hero,
                    **self.player_stats(acc, ctx.hist_pre),
                }
                if hero is not None:
                    st = model.hist.players.get(acc)
                    p["hero_games"] = st.hero_games.get(hero, 0) if st else 0
                    p["hero_wins"] = st.hero_wins.get(hero, 0) if st else 0
                    p["comfort"] = round(comfort(model.hist, acc, hero, model.comfort_center), 3)
                    p["hero_strength"] = round(hero_strength(model.hist, hero, model.params), 3)
                players.append(p)
            sides[side] = {"key": key, "name": team.name, "players": players,
                           "picks": [(acc, h) for acc, h in zip(lineup, heroes[:5])],
                           **self.team_rating(ctx, lineup)}

        pre = sides["a"]["rating"] - sides["b"]["rating"]
        n_heroes = sum(1 for s in sides.values() for _, h in s["picks"] if h is not None)
        draft = None
        if n_heroes:
            draft = draft_delta(model.hist, sides["a"]["picks"], sides["b"]["picks"],
                                model.params, model.comfort_center)
        final = model.combine(pre, draft)

        def cap(p: float) -> float:
            return min(max(p, 1 - CONFIDENCE_CAP), CONFIDENCE_CAP)

        p_pre, p_final = cap(sigmoid(pre)), cap(sigmoid(final))
        for s in sides.values():
            s.pop("picks")
            s["rating"] = round(s["rating"], 4)
        out["teams"] = sides
        out["pre_draft"] = {"p_a": round(p_pre, 4), "logit": round(pre, 4)}
        out["draft"] = None if draft is None else {
            "p_a": round(p_final, 4), "heroes": n_heroes,
            "delta": {"hero_wr": round(draft[0], 4), "comfort": round(draft[1], 4)},
            "shift": round(p_final - p_pre, 4)}
        out["p_a"] = round(p_final, 4)
        out["winner"] = "a" if p_final >= 0.5 else "b"
        out["fair_odds"] = {"a": round(1 / p_final, 3), "b": round(1 / (1 - p_final), 3)}
        out["series"] = series_probs(p_final)
        return out

    def hero_table(self) -> list[dict]:
        model = self.model
        out = []
        for h in hero_by_id().values():
            hs = model.hist.heroes.get(h["id"])
            out.append({"hero_id": h["id"], "games": hs.games if hs else 0,
                        "wins": hs.wins if hs else 0,
                        "strength": round(hero_strength(model.hist, h["id"], model.params), 4),
                        "farm_rank": round(hs.rank_sum / hs.games, 2) if hs and hs.games else None})
        return out


def series_probs(p: float) -> dict:
    """Серия из трёх игр (в супермиксерах играются все три) и до двух побед."""
    q = 1 - p
    return {
        "bo3_a": round(p * p + 2 * p * p * q, 4),
        "three_games": {"3:0": round(p ** 3, 4), "2:1": round(3 * p * p * q, 4),
                        "1:2": round(3 * p * q * q, 4), "0:3": round(q ** 3, 4)},
    }
