from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from predicator import live as live_mod
from predicator.live import assign_heroes, link_game, live_url
from predicator.rosters import load_teams
from predicator.web import create_app
from synthetic import CURRENT_CUP, HERO_IDS


def make_game(radiant: list[int], dire: list[int], heroes: dict[int, int] | None = None,
              picks: dict[bool, list[int]] | None = None, game_time: int = 0, match_id: int = 777) -> dict:
    heroes = heroes or {}
    players = [{"account_id": a, "hero_id": heroes.get(a, 0), "is_radiant": True} for a in radiant]
    players += [{"account_id": a, "hero_id": heroes.get(a, 0), "is_radiant": False} for a in dire]
    pb = [{"hero_id": h, "is_pick": True, "is_radiant": side, "side_order": i}
          for side, hs in (picks or {}).items() for i, h in enumerate(hs)]
    return {"league_id": 19924, "match_id": match_id, "game_time": game_time, "radiant_score": 0,
            "dire_score": 0, "players": players, "picks_bans": pb}


def test_live_url_follows_local_export(settings):
    assert live_url(settings) is None
    local = replace(settings, backup_url="http://127.0.0.1:8000/api/export/backup")
    assert live_url(local) == "http://127.0.0.1:8000/api/export/live"
    assert live_url(replace(local, live_url="http://x/live")) == "http://x/live"


def test_link_by_mixer_game_and_by_roster(conn):
    teams = {t.key: t for t in load_teams(conn, CURRENT_CUP)}
    a, b = list(teams.values())[:2]
    game = make_game(b.lineup[:5], a.lineup[:5])
    row = {"match_id": 777, "team1_key": a.key, "team2_key": b.key}
    assert link_game(game, teams, [row]) == {"radiant": b.key, "dire": a.key}
    # mixer-cup ещё не знает match_id: ищем по составу, нужно 3 из 5
    assert link_game(game, teams) == {"radiant": b.key, "dire": a.key}
    stranger = make_game([10**9 + i for i in range(5)], a.lineup[:5])
    assert link_game(stranger, teams) is None


def test_game_clock_is_whole_seconds(conn):
    teams = {t.key: t for t in load_teams(conn, CURRENT_CUP)}
    a, b = list(teams.values())[:2]
    from predicator.live import describe
    g = describe(make_game(a.lineup[:5], b.lineup[:5], game_time=1090.0667), {"radiant": a.key, "dire": b.key},
                 lambda acc, hero: 0.0)
    assert g["game_time"] == 1090 and g["before_horn"] is False


def test_heroes_known_and_provisional():
    game = make_game([1, 2, 3, 4, 5], [6, 7, 8, 9, 10], heroes={1: 11, 6: 12},
                     picks={True: [11, 21, 22], False: [12, 31]})
    # игрок 3 лучше всех знает героя 22, игрок 2 — героя 21
    comfort = {(3, 22): 5.0, (2, 21): 4.0, (2, 22): 1.0}.get
    out = {x["account_id"]: (x["hero_id"], x["provisional"])
           for x in assign_heroes(game, lambda a, h: comfort((a, h), 0.0))}
    assert out[1] == (11, False) and out[6] == (12, False)
    assert out[3] == (22, True) and out[2] == (21, True)
    assert sum(1 for _, p in out.values() if p) == 3          # 31 у кого-то из тьмы
    assert len({h for h, _ in out.values()}) == len(out)       # героев не раздали дважды


def live_client(conn, settings, monkeypatch, game_time):
    teams = load_teams(conn, CURRENT_CUP)
    a, b = teams[0], teams[1]
    heroes = dict(zip(a.lineup[:5] + b.lineup[:5], HERO_IDS[:10]))
    game = make_game(a.lineup[:5], b.lineup[:5], heroes=heroes,
                     picks={True: HERO_IDS[:5], False: HERO_IDS[5:10]}, game_time=game_time)
    monkeypatch.setattr(live_mod.LiveFeed, "games", lambda self: [game])
    app = create_app(replace(settings, backup_url="http://127.0.0.1:8000/api/export/backup"),
                     start_sync=False)
    return TestClient(app), a, b, heroes


def test_live_endpoint_returns_linked_draft(conn, settings, monkeypatch):
    client, a, b, heroes = live_client(conn, settings, monkeypatch, game_time=0)
    with client:
        data = client.get("/api/live", params={"tournament_id": CURRENT_CUP}).json()
    assert data["enabled"] is True and len(data["games"]) == 1
    g = data["games"][0]
    assert g["team_keys"] == {"radiant": a.key, "dire": b.key}
    assert g["draft_complete"] and g["before_horn"] and g["assigned"] == 10
    assert {x["account_id"]: x["hero_id"] for x in g["heroes"]} == heroes


def predict_live(client, a, b, heroes):
    return client.post("/api/predict", json={
        "tournament_id": CURRENT_CUP, "team_a": a.key, "team_b": b.key,
        "heroes_a": [heroes[x] for x in a.lineup[:5]], "heroes_b": [heroes[x] for x in b.lineup[:5]],
        "k_a": 5.0, "k_b": 1.15, "in_play": True}).json()


def test_value_allowed_before_horn_with_full_draft(conn, settings, monkeypatch):
    client, a, b, heroes = live_client(conn, settings, monkeypatch, game_time=0)
    with client:
        res = predict_live(client, a, b, heroes)
    assert res["live"]["before_horn"] and res["offer"]["draft_window"] is True
    assert res["offer"]["in_play"] is False
    side = res["offer"]["sides"]["a"]
    assert side["value"] == (side["ev"] >= 0.03)


def test_value_suppressed_after_horn(conn, settings, monkeypatch):
    client, a, b, heroes = live_client(conn, settings, monkeypatch, game_time=420)
    with client:
        res = predict_live(client, a, b, heroes)
    assert res["live"]["before_horn"] is False and res["offer"]["draft_window"] is False
    assert res["offer"]["in_play"] is True and res["offer"]["best"] is None
