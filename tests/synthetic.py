"""Синтетический бэкап в формате донора: игроки со скрытой силой, кубки, драфты.

Исход игры зависит от суммы сил пятёрок, доля золота — от силы игрока, поэтому
модель на этих данных обязана угадывать заметно лучше монетки.
"""

from __future__ import annotations

import math
import random
import time

HERO_IDS = [1, 2, 5, 8, 11, 14, 17, 22, 26, 35, 41, 44, 53, 67, 74, 86, 93, 106, 129, 145]
LEAGUE = 19924
CURRENT_CUP = 40


def make_backup(n_cups: int = 3, teams_per_cup: int = 16, n_players: int = 110,
                seed: int = 7) -> dict:
    rng = random.Random(seed)
    base = int(time.time()) - 120 * 86400
    skill = {}
    players = []
    for i in range(n_players):
        acc = 1000 + i
        skill[acc] = rng.gauss(0, 1)
        players.append({"account_id": acc, "name": f"player{i}",
                        "mmr": round(6000 + 900 * skill[acc] + rng.gauss(0, 300)),
                        "preferred_roles": rng.choice(["CARRY", "MIDLANER", "OFFLANER",
                                                       "SOFT_SUPPORT,HARD_SUPPORT"]),
                        "team_id": None, "roster_confirmed": False})

    matches, match_players, drafts, teams = [], {}, {}, []
    match_id = 9_000_000_000
    for c in range(n_cups):
        tid = 26 + c
        pool = rng.sample(list(skill), teams_per_cup * 5)
        rosters = {7000 + c * 100 + t: pool[t * 5:(t + 1) * 5] for t in range(teams_per_cup)}
        start = base + c * 20 * 86400
        pairs = [(a, b) for a in rosters for b in rosters if a < b]
        rng.shuffle(pairs)
        for n, (ta, tb) in enumerate(pairs):
            match_id += 1
            ra, rb = rosters[ta], rosters[tb]
            diff = sum(skill[p] for p in ra) - sum(skill[p] for p in rb)
            radiant_win = rng.random() < 1 / (1 + math.exp(-0.6 * diff))
            heroes = rng.sample(HERO_IDS, 10)
            rows = []
            for side, (team, roster) in enumerate(((ta, ra), (tb, rb))):
                won = radiant_win if side == 0 else not radiant_win
                for k, acc in enumerate(roster):
                    gpm = max(150, int(420 + 90 * skill[acc] + (60 if won else 0) + rng.gauss(0, 60)))
                    rows.append([acc, heroes[side * 5 + k], team, side == 0, rng.randint(0, 12),
                                 rng.randint(0, 10), rng.randint(0, 20), gpm, gpm + 50, gpm * 35])
            matches.append([match_id, LEAGUE, start + n * 3600, 2100, ta, tb, radiant_win, tid])
            match_players[str(match_id)] = rows
            drafts[str(match_id)] = [[i, h, ta if i % 2 == 0 else tb, 1] for i, h in enumerate(heroes)]
        for team in rosters:
            teams.append({"team_id": team, "name": f"Team c{c}t{team % 100}", "mixer_uuid": None,
                          "tournament_id": tid})

    # текущий кубок: составы есть, игр ещё нет
    pool = rng.sample(list(skill), 8 * 5)
    for t in range(8):
        team_id = 2_000_000_000 + t
        teams.append({"team_id": team_id, "name": f"Team now{t}", "mixer_uuid": f"uuid-{t}",
                      "tournament_id": CURRENT_CUP})
        for acc in pool[t * 5:(t + 1) * 5]:
            p = next(x for x in players if x["account_id"] == acc)
            p["team_id"] = team_id
            p["roster_confirmed"] = True
    queued = [{"player_uuid": f"q-{i}", "nickname": f"queue{i}", "rating": 5000 + 100 * i,
               "queue_position": i + 1, "updated_at": "2026-10-01T00:00:00+00:00"} for i in range(5)]
    queued.append({"player_uuid": "q-dup", "nickname": "player3", "rating": 7000,
                   "queue_position": 9, "updated_at": "2026-10-01T00:00:00+00:00"})
    return {"matches": matches, "match_players": match_players, "match_drafts": drafts,
            "players": players, "teams": teams, "queued_players": queued,
            "_skill": skill}


def team_skill(backup: dict, team_id: int) -> float:
    skill = backup["_skill"]
    return sum(skill[p["account_id"]] for p in backup["players"] if p["team_id"] == team_id)
