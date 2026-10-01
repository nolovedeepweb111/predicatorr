"""Живые игры кубка: драфт и время игры из выгрузки pari-mixer (/api/export/live).

pari-mixer спрашивает Steam GetLiveLeagueGames раз в 15 секунд (ключ остаётся у
него) и отдаёт составы, пики и баны ещё до горна. Здесь игра сопоставляется с
командами кубка, а пики — с игроками. В Captains Mode капитан сначала выбирает героев
на всю команду, а кто на ком играет, становится известно позже. Пока героя не взял
ни один игрок, назначаем его предварительно тому, у кого на нём больше всего игр.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from typing import Iterable

from .config import Settings
from .http import FetchError, get_json

CACHE_SECONDS = 15
MAX_AGE = 120          # старше — игра могла закончиться, не показываем


def live_url(settings: Settings) -> str | None:
    if settings.live_url:
        return settings.live_url
    if settings.backup_url.endswith("/api/export/backup"):
        return settings.backup_url[: -len("backup")] + "live"
    return None


class LiveFeed:
    """Кэш живых игр: не чаще раза в 15 секунд, токен — тот же, что у выгрузки."""

    def __init__(self, settings: Settings) -> None:
        self.url = live_url(settings)
        self.token = settings.backup_token
        self._lock = threading.Lock()
        self._data: dict = {}
        self._asked_at = 0.0
        self._got_at = 0.0
        self.error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def games(self) -> list[dict]:
        if not self.url:
            return []
        with self._lock:
            now = time.time()
            if now - self._asked_at >= CACHE_SECONDS:
                self._asked_at = now
                try:
                    data = get_json(self.url, timeout=10,
                                    headers={"X-Export-Token": self.token} if self.token else None)
                    self._data, self._got_at = data if isinstance(data, dict) else {}, now
                    self.error = str(self._data.get("error"))[:300] if self._data.get("stale") else None
                except FetchError as exc:
                    self.error = str(exc)[:300]
            if now - self._got_at > MAX_AGE:
                return []
            return list(self._data.get("games") or [])

    def status(self) -> dict:
        return {"enabled": self.enabled, "error": self.error,
                "fetched_at": int(self._got_at) or None}


def _sides(game: dict) -> tuple[list[int], list[int]]:
    players = [p for p in game.get("players") or [] if p.get("account_id")]
    return ([int(p["account_id"]) for p in players if p.get("is_radiant")],
            [int(p["account_id"]) for p in players if not p.get("is_radiant")])


def link_game(game: dict, teams: dict, active: Iterable[sqlite3.Row] = ()) -> dict[str, str] | None:
    """Какая команда кубка за свет, какая за тьму: {"radiant": key, "dire": key}.

    Пару даёт игра mixer-cup с тем же match_id, а стороны — пересечение составов. Если
    в mixer-cup игры ещё нет, команда ищется по составу целиком (нужно 3 из 5).
    """
    radiant, dire = map(set, _sides(game))
    keys = None
    for r in active:
        if r["match_id"] and int(r["match_id"]) == int(game.get("match_id") or 0):
            keys = [k for k in (r["team1_key"], r["team2_key"]) if k in teams]
            break

    def overlap(key: str, side: set[int]) -> int:
        return len(side & set(teams[key].lineup))

    if keys and len(keys) == 2:
        a, b = keys
        if overlap(a, radiant) + overlap(b, dire) >= overlap(a, dire) + overlap(b, radiant):
            return {"radiant": a, "dire": b}
        return {"radiant": b, "dire": a}
    best = {}
    for side, accs in (("radiant", radiant), ("dire", dire)):
        scored = sorted(((overlap(k, accs), k) for k in teams), reverse=True)
        if not scored or scored[0][0] < 3:
            return None
        best[side] = scored[0][1]
    return best if best["radiant"] != best["dire"] else None


def assign_heroes(game: dict, comfort) -> list[dict]:
    """Герой каждого игрока: известный из Steam или предварительный.

    comfort(account_id, hero_id) — насколько игрок знаком с героем (больше — вероятнее).
    """
    players = [p for p in game.get("players") or [] if p.get("account_id")]
    out = [{"account_id": int(p["account_id"]), "hero_id": int(p["hero_id"]), "provisional": False}
           for p in players if p.get("hero_id")]
    taken = {(p["account_id"]) for p in out}
    for radiant in (True, False):
        picked = [int(x["hero_id"]) for x in game.get("picks_bans") or []
                  if x.get("is_pick") and bool(x.get("is_radiant")) == radiant and x.get("hero_id")]
        have = {int(p["hero_id"]) for p in players if p.get("hero_id") and bool(p.get("is_radiant")) == radiant}
        heroes = [h for h in picked if h not in have]
        free = [int(p["account_id"]) for p in players
                if bool(p.get("is_radiant")) == radiant and int(p["account_id"]) not in taken]
        pairs = sorted(((comfort(a, h), a, h) for a in free for h in heroes), reverse=True)
        used_a, used_h = set(), set()
        for _, a, h in pairs:
            if a in used_a or h in used_h:
                continue
            used_a.add(a)
            used_h.add(h)
            out.append({"account_id": a, "hero_id": h, "provisional": True})
    return out


def describe(game: dict, sides: dict[str, str], comfort) -> dict:
    """То, что нужно странице прогноза: команды, время, драфт, герои по игрокам."""
    radiant, dire = _sides(game)
    picks = [x for x in game.get("picks_bans") or [] if x.get("is_pick") and x.get("hero_id")]
    heroes = assign_heroes(game, comfort)
    return {
        "match_id": game.get("match_id"), "league_id": game.get("league_id"),
        "team_keys": sides, "lineups": {"radiant": radiant, "dire": dire},
        "game_time": game.get("game_time"),
        "score": [game.get("radiant_score"), game.get("dire_score")],
        "picks": len(picks), "bans": sum(1 for x in game.get("picks_bans") or [] if not x.get("is_pick")),
        "heroes": heroes,
        "assigned": sum(1 for h in heroes if not h["provisional"]),
        "draft_complete": len(picks) >= 10,
        "before_horn": game.get("game_time") is not None and game["game_time"] <= 0,
    }


def comfort_fn(hist, ext):
    """Знакомство игрока с героем: игры в лиге важнее, рейтинг — следом."""
    def comfort(acc: int, hero: int) -> float:
        st = hist.players.get(acc)
        league = st.hero_games.get(hero, 0) if st else 0
        ranked = 0.0
        if ext is not None:
            ph = ext.player_hero(acc, hero)
            ranked = ph[0].games if ph else 0.0
        return 2 * math.log1p(league) + math.log1p(ranked)
    return comfort
