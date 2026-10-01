"""Прогнозы для живых составов: склейка модели, истории и текущих команд."""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Sequence

from .config import Settings
from .dataset import Dataset, load_dataset
from .db import connect, get_meta
from .external import ExternalData
from .features import (ROSTER_FEATURES, History, Imputation, default_imputation, roster_features,
                       sigmoid)
from .heroes import hero_by_id
from .live import LiveFeed, comfort_fn, describe, link_game
from .model import (CupRater, Norm, TrainedModel, comfort, draft_delta, hero_strength,
                    match_picks, roster_score, train)
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
        self._played_cache: dict[tuple, list[dict]] = {}

    def connect(self) -> sqlite3.Connection:
        return connect(self.settings.db_path)

    # --- модель ----------------------------------------------------------

    def refresh(self, conn: sqlite3.Connection, force: bool = False) -> None:
        # переобучаемся и на новый бэкап, и на новые внешние данные (мета, матчапы, игроки)
        version = f'{get_meta(conn, "data_version", "0")}/{get_meta(conn, "external_version", "0")}'
        with self._lock:
            if not force and version == self._version and self._ds is not None:
                return
            ds = load_dataset(conn)
            ext = ExternalData.load(conn) if self.settings.external_enabled else None
            try:
                model = train(ds, int(time.time()), ext=ext)
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
            "draft_features": list(m.draft_features) if m else [],
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
            "gold": round(st.gold_sum / st.stat_games, 3) if st.stat_games else None,
            "elo": round(st.elo),
            "durability": None if hist.durability(account_id) is None
            else round(hist.durability(account_id), 3),
            "farm_rank": round(st.rank_sum / st.stat_games, 2) if st.stat_games else None,
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
                    ph = model.ext.player_hero(acc, hero) if model.ext is not None else None
                    if ph is not None:
                        stats, _ = ph
                        p["pub"] = {"games": stats.games_career, "wins": stats.wins_career,
                                    "games_recent": stats.games_recent,
                                    "wins_recent": stats.wins_recent}
                players.append(p)
            sides[side] = {"key": key, "name": team.name, "players": players,
                           "picks": [(acc, h) for acc, h in zip(lineup, heroes[:5])],
                           **self.team_rating(ctx, lineup)}

        pre = sides["a"]["rating"] - sides["b"]["rating"]
        n_heroes = sum(1 for s in sides.values() for _, h in s["picks"] if h is not None)
        draft = None
        if n_heroes:
            draft = draft_delta(model.hist, sides["a"]["picks"], sides["b"]["picks"],
                                model.params, model.comfort_center, model.ext, int(time.time()))
        final = model.combine(pre, draft)

        def cap(p: float) -> float:
            return min(max(p, 1 - CONFIDENCE_CAP), CONFIDENCE_CAP)

        p_pre, p_final = cap(sigmoid(pre)), cap(sigmoid(final))
        picks = {k: s.pop("picks") for k, s in sides.items()}
        for s in sides.values():
            s["rating"] = round(s["rating"], 4)
        out["teams"] = sides
        out["pre_draft"] = {"p_a": round(p_pre, 4), "logit": round(pre, 4)}
        out["draft"] = None if draft is None else {
            "p_a": round(p_final, 4), "heroes": n_heroes,
            "delta": {k: round(v, 4) for k, v in draft.items() if k in model.draft_features},
            # во что превращается каждый признак: сдвиг вероятности A в п.п. при прочих равных
            "effects": {k: round(sigmoid(model.draft_coef[0] * pre + v) - sigmoid(model.draft_coef[0] * pre), 4)
                        for k, v in model.contributions(draft).items()},
            "matchups": self._matchup_notes(model, picks["a"], picks["b"]),
            "shift": round(p_final - p_pre, 4)}
        out["p_a"] = round(p_final, 4)
        out["winner"] = "a" if p_final >= 0.5 else "b"
        out["fair_odds"] = {"a": round(1 / p_final, 3), "b": round(1 / (1 - p_final), 3)}
        out["series"] = series_probs(p_final)
        return out

    def live_games(self, conn: sqlite3.Connection, tournament_id: int, feed: LiveFeed) -> list[dict]:
        """Идущие игры кубка с командами, временем и героями по игрокам."""
        games = feed.games()
        if not games:
            return []
        ctx = self.cup_context(conn, tournament_id)
        active = conn.execute("SELECT match_id, team1_key, team2_key FROM live_games"
                              " WHERE tournament_id = ? AND match_id IS NOT NULL",
                              (tournament_id,)).fetchall()
        comfort = comfort_fn(self.model.hist, self.model.ext)
        out = []
        for g in games:
            sides = link_game(g, ctx.teams, active)
            if sides:
                out.append(describe(g, sides, comfort))
        return out

    def played_games(self, conn: sqlite3.Connection, tournament_id: int) -> list[dict]:
        """Сыгранные игры кубка: прогноз до драфта и с драфтом и что вышло.

        Только то, что было известно до игры: признаки состава — по истории до кубка,
        форма — по прошлым играм кубка, драфт — по истории до этой игры, составы и
        герои — те, что реально вышли. Модель обучена на завершённых кубках, а идущий в
        неё не входит, так что это честный прогноз, а не подгонка.
        """
        ctx = self.cup_context(conn, tournament_id)
        ds, model = self.dataset, self.model
        cup = ds.cups.get(tournament_id)
        if cup is None:
            return []
        key = (self._version, tournament_id, len(cup.matches))
        with self._lock:
            if key in self._played_cache:
                return self._played_cache[key]
        params = model.params
        wanted = {m.match_id for m in cup.matches}
        drafts: dict[int, dict] = {}
        hist = History()
        center_sum = center_n = 0.0
        for m in ds.matches:                     # драфт — по истории строго до игры
            if m.match_id in wanted:
                center = center_sum / center_n if center_n else 0.0
                drafts[m.match_id] = draft_delta(hist, match_picks(m, True), match_picks(m, False),
                                                 params, center, model.ext, m.start_time)
            if len(m.radiant) == 5 and len(m.dire) == 5:
                for p in m.radiant + m.dire:
                    st = hist.players.get(p.account_id)
                    center_sum += math.log1p(st.hero_games.get(p.hero_id, 0) if st else 0)
                    center_n += 1
            hist.add(m)
        rows = {r["match_id"]: r for r in conn.execute(
            "SELECT match_id, team1_key, team2_key, planned_time, start_time FROM live_games"
            " WHERE tournament_id = ? AND match_id IS NOT NULL", (tournament_id,))}

        def cap(p: float) -> float:
            return min(max(p, 1 - CONFIDENCE_CAP), CONFIDENCE_CAP)

        rater = CupRater(params)
        out = []
        for m in cup.matches:
            rating = {}
            for radiant in (True, False):
                lineup = sorted(m.lineup(radiant))
                feats = roster_features(ctx.hist_pre, lineup, ctx.mmr, ctx.imp, params.gold_shrink)
                rating[radiant] = (params.scale * roster_score(model.weights, ctx.norm, feats)
                                   + rater.form(lineup)[0])
            z = rating[True] - rating[False]
            rater.add(m.lineup(True), m.lineup(False), bool(m.radiant_win), sigmoid(z))
            row = rows.get(m.match_id)
            sides = _game_sides(m, row, ctx.teams)
            a_radiant = sides["a_radiant"]
            p_pre = sigmoid(z)
            p_draft = sigmoid(model.combine(z, drafts[m.match_id]))
            if not a_radiant:
                p_pre, p_draft = 1 - p_pre, 1 - p_draft
            heroes = {r: [p.hero_id for p in m.side(r) if p.hero_id] for r in (True, False)}
            out.append({
                "match_id": m.match_id,
                "start_time": (row["start_time"] if row and row["start_time"] else m.start_time),
                "duration": m.duration,
                "team_a": sides["a"], "team_b": sides["b"],
                "winner": "a" if bool(m.radiant_win) == a_radiant else "b",
                "pre": round(cap(p_pre), 4), "draft": round(cap(p_draft), 4),
                "heroes_a": heroes[a_radiant], "heroes_b": heroes[not a_radiant],
            })
        out.reverse()                            # свежие сверху
        with self._lock:
            self._played_cache = {key: out}
        return out

    def _matchup_notes(self, model: TrainedModel, a: list, b: list) -> list[dict]:
        """Самые заметные матчапы драфта (перевес героя A над героем B по высокому рейтингу)."""
        ext = model.ext
        if ext is None or not ext.available()["vs"]:
            return []
        now = int(time.time())
        ha = [h for _, h in a if h is not None]
        hb = [h for _, h in b if h is not None]
        notes = [{"a": x, "b": y, "adv": round(ext.advantage(x, y, now), 4)} for x in ha for y in hb]
        notes = [n for n in notes if abs(n["adv"]) >= 0.01]
        return sorted(notes, key=lambda n: -abs(n["adv"]))[:6]

    def hero_table(self) -> list[dict]:
        model = self.model
        out = []
        for h in hero_by_id().values():
            hs = model.hist.heroes.get(h["id"])
            meta = None
            if model.ext is not None and model.ext.available()["meta"]:
                meta = round(sigmoid(model.ext.hero_meta(h["id"], int(time.time()))), 4)
            out.append({"hero_id": h["id"], "games": hs.games if hs else 0,
                        "wins": hs.wins if hs else 0,
                        "strength": round(hero_strength(model.hist, h["id"], model.params), 4),
                        "meta_wr": meta,
                        "farm_rank": round(hs.rank_sum / hs.stat_games, 2) if hs and hs.stat_games else None})
        return out


def _game_sides(m, row, teams: dict) -> dict:
    """Команды игры: из расписания mixer-cup (team1 — «A»), стороны — по составам."""
    radiant, dire = m.lineup(True), m.lineup(False)

    def overlap(key: str | None, side: frozenset[int]) -> int:
        return len(side & set(teams[key].lineup)) if key in teams else 0

    def info(key: str | None, fallback: str) -> dict:
        return {"key": key, "name": teams[key].name if key in teams else fallback}

    if row and row["team1_key"] in teams and row["team2_key"] in teams:
        t1, t2 = row["team1_key"], row["team2_key"]
        a_radiant = overlap(t1, radiant) + overlap(t2, dire) >= overlap(t1, dire) + overlap(t2, radiant)
        return {"a": info(t1, ""), "b": info(t2, ""), "a_radiant": a_radiant}
    best = {}
    for name, side in (("radiant", radiant), ("dire", dire)):
        score, key = max(((overlap(k, side), k) for k in teams), default=(0, None))
        best[name] = key if score >= 3 else None
    return {"a": info(best["radiant"], "Свет"), "b": info(best["dire"], "Тьма"), "a_radiant": True}


def series_probs(p: float) -> dict:
    """Серия из трёх игр (в супермиксерах играются все три) и до двух побед."""
    q = 1 - p
    return {
        "bo3_a": round(p * p + 2 * p * p * q, 4),
        "three_games": {"3:0": round(p ** 3, 4), "2:1": round(3 * p * p * q, 4),
                        "1:2": round(3 * p * q * q, 4), "0:3": round(q ** 3, 4)},
    }
