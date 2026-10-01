from __future__ import annotations

import time

from fastapi.testclient import TestClient

from predicator.importer import bump_data_version
from predicator.rosters import load_teams
from predicator.service import PredictorService
from predicator.web import create_app
from synthetic import CURRENT_CUP, HERO_IDS

NOW = int(time.time())


def add_played(conn, radiant, dire, radiant_win: bool, start: int, match_id: int, team1_is_radiant: bool):
    conn.execute("INSERT INTO matches(match_id, league_id, start_time, duration, radiant_team_id,"
                 " dire_team_id, radiant_win, tournament_id) VALUES (?,?,?,?,?,?,?,?)",
                 (match_id, 19924, start, 2000, radiant.team_ids[0], dire.team_ids[0],
                  int(radiant_win), CURRENT_CUP))
    for is_radiant, team, offset in ((1, radiant, 0), (0, dire, 5)):
        for k, acc in enumerate(team.lineup[:5]):
            conn.execute("INSERT INTO match_players(match_id, account_id, hero_id, team_id, is_radiant,"
                         " kills, deaths, assists, gold_per_min, xp_per_min, net_worth)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (match_id, acc, HERO_IDS[offset + k], team.team_ids[0], is_radiant,
                          5, 5, 5, 400, 450, 14000))
    t1, t2 = (radiant, dire) if team1_is_radiant else (dire, radiant)
    won1 = radiant_win == team1_is_radiant
    conn.execute("INSERT INTO live_games(game_id, tournament_id, status, match_id, result, team1_key,"
                 " team2_key, seq, planned_time, start_time, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (f"g{match_id}", CURRENT_CUP, "COMPLETE", match_id, "WIN1" if won1 else "WIN2",
                  t1.key, t2.key, match_id % 100, start, start, NOW))


def setup_cup(conn):
    teams = load_teams(conn, CURRENT_CUP)
    a, b, c = teams[0], teams[1], teams[2]
    add_played(conn, a, b, True, NOW - 7200, 9_900_000_001, team1_is_radiant=False)
    add_played(conn, a, c, False, NOW - 3600, 9_900_000_002, team1_is_radiant=True)
    conn.execute("INSERT INTO live_games(game_id, tournament_id, status, team1_key, team2_key, seq,"
                 " planned_time, synced_at) VALUES (?,?,?,?,?,?,?,?)",
                 ("next2", CURRENT_CUP, "PENDING", teams[4].key, teams[5].key, 4, NOW + 7200, NOW))
    conn.execute("INSERT INTO live_games(game_id, tournament_id, status, team1_key, team2_key, seq,"
                 " planned_time, synced_at) VALUES (?,?,?,?,?,?,?,?)",
                 ("next1", CURRENT_CUP, "PENDING", teams[2].key, teams[3].key, 3, NOW + 3600, NOW))
    bump_data_version(conn)
    return teams


def test_played_games_replay(conn, settings):
    teams = setup_cup(conn)
    a, b, c = teams[0], teams[1], teams[2]
    games = PredictorService(settings).played_games(conn, CURRENT_CUP)
    assert [g["match_id"] for g in games] == [9_900_000_002, 9_900_000_001]     # свежие сверху
    first = games[1]
    # team1 в расписании — b (тьма), поэтому «A» — это b, и выиграл свет (a) = «B»
    assert first["team_a"]["key"] == b.key and first["team_b"]["key"] == a.key
    assert first["winner"] == "b"
    assert first["heroes_a"] == HERO_IDS[5:10] and first["heroes_b"] == HERO_IDS[:5]
    second = games[0]
    assert second["team_a"]["key"] == a.key and second["winner"] == "b"
    for g in games:
        assert 0 < g["pre"] < 1 and 0 < g["draft"] < 1 and g["pre"] != g["draft"]


def test_games_api_played_and_upcoming(conn, settings):
    teams = setup_cup(conn)
    a, b = teams[0], teams[1]
    # PARI перед первой игрой: a — фаворит (1.5), и a выиграл
    for fetched, k1, k2 in ((NOW - 9000, 1.9, 1.9), (NOW - 7300, 2.6, 1.5)):
        conn.execute("INSERT INTO odds_snapshots(event_id, fetched_at, team1, team2, start_time, k1, k2,"
                     " team1_key, team2_key) VALUES (?,?,?,?,?,?,?,?,?)",
                     (77, time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(fetched)), b.name, a.name,
                      NOW - 7200, k1, k2, b.key, a.key))
    app = create_app(settings, start_sync=False)
    with TestClient(app) as client:
        played = client.get("/api/games", params={"kind": "played", "tournament_id": CURRENT_CUP}).json()
        assert played["summary"]["games"] == 2 and played["summary"]["pari_games"] == 1
        assert played["summary"]["pari_hits"] == 1
        g = next(x for x in played["games"] if x["match_id"] == 9_900_000_001)
        assert g["pari"] == {"opening": [1.9, 1.9], "close": [2.6, 1.5]}
        up = client.get("/api/games", params={"kind": "upcoming", "tournament_id": CURRENT_CUP}).json()
        assert [x["game_id"] for x in up["games"]] == ["next1", "next2"]          # по времени
        assert all(0 < x["p_a"] < 1 and x["team_a"]["name"] for x in up["games"])
        assert client.get("/api/games", params={"kind": "bad"}).status_code == 400
