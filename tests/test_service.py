from __future__ import annotations

import pytest

from predicator.rosters import (add_change, delete_change, ensure_local_player, load_teams,
                                search_players, tournaments)
from predicator.service import PredictorService
from synthetic import CURRENT_CUP, team_skill


def test_current_tournament_is_default(conn):
    ts = tournaments(conn)
    assert ts[0]["id"] == CURRENT_CUP
    assert ts[0]["confirmed"] == 40
    teams = load_teams(conn, CURRENT_CUP)
    assert len(teams) == 8 and all(len(t.players) == 5 for t in teams)


def test_prediction_favours_stronger_team(conn, settings, backup):
    svc = PredictorService(settings)
    teams = load_teams(conn, CURRENT_CUP)
    by_skill = sorted(teams, key=lambda t: team_skill(backup, t.team_ids[0]))
    weak, strong = by_skill[0], by_skill[-1]
    res = svc.predict(conn, CURRENT_CUP, strong.key, weak.key)
    assert res["p_a"] > 0.6
    assert res["winner"] == "a"
    swapped = svc.predict(conn, CURRENT_CUP, weak.key, strong.key)
    assert abs(swapped["p_a"] - (1 - res["p_a"])) < 1e-3
    assert res["draft"] is None


def test_prediction_with_partial_draft(conn, settings):
    svc = PredictorService(settings)
    teams = load_teams(conn, CURRENT_CUP)
    a, b = teams[0], teams[1]
    res = svc.predict(conn, CURRENT_CUP, a.key, b.key, heroes_a=[1, None, None, None, None])
    assert res["draft"]["heroes"] == 1
    assert 0.05 < res["p_a"] < 0.95
    hero_player = res["teams"]["a"]["players"][0]
    assert hero_player["hero_id"] == 1 and "hero_games" in hero_player


def test_prediction_rejects_bad_lineup(conn, settings):
    svc = PredictorService(settings)
    teams = load_teams(conn, CURRENT_CUP)
    with pytest.raises(ValueError):
        svc.predict(conn, CURRENT_CUP, teams[0].key, teams[1].key, lineup_a=teams[0].lineup[:4])


def test_substitution_apply_and_undo(conn, settings):
    svc = PredictorService(settings)
    team = load_teams(conn, CURRENT_CUP)[0]
    other = load_teams(conn, CURRENT_CUP)[1]
    before = svc.predict(conn, CURRENT_CUP, team.key, other.key)["p_a"]

    out_acc = team.lineup[0]
    newcomer = ensure_local_player(conn, "свежий новичок", 9500)
    assert newcomer < 0
    change = add_change(conn, CURRENT_CUP, team.key, out_acc, newcomer, "болеет")
    updated = next(t for t in load_teams(conn, CURRENT_CUP) if t.key == team.key)
    assert newcomer in updated.lineup and out_acc not in updated.lineup
    assert updated.players[0].substitute and updated.changes[0]["applied"]
    after = svc.predict(conn, CURRENT_CUP, team.key, other.key)["p_a"]
    assert after != before

    with pytest.raises(ValueError):
        add_change(conn, CURRENT_CUP, team.key, out_acc, newcomer)   # уже не в составе

    assert delete_change(conn, change)
    restored = next(t for t in load_teams(conn, CURRENT_CUP) if t.key == team.key)
    assert restored.lineup == team.lineup


def test_change_becomes_noop_when_source_catches_up(conn):
    team = load_teams(conn, CURRENT_CUP)[0]
    out_acc = team.lineup[1]
    newcomer = ensure_local_player(conn, "из очереди", 6000)
    add_change(conn, CURRENT_CUP, team.key, out_acc, newcomer)
    # источник сам узнал о замене
    conn.execute("UPDATE players SET team_id = NULL WHERE account_id = ?", (out_acc,))
    conn.execute("UPDATE players SET team_id = ? WHERE account_id = ?", (team.team_ids[0], newcomer))
    t = next(t for t in load_teams(conn, CURRENT_CUP) if t.key == team.key)
    assert newcomer in t.lineup and len(t.lineup) == 5
    assert t.changes[0]["applied"] is False


def test_search_merges_players_and_queue(conn):
    res = search_players(conn, "player3", CURRENT_CUP)
    first = res[0]
    assert first["name"] == "player3" and first["source"] == "player"
    assert first["queue_position"] == 9          # тот же ник стоит в очереди
    queue = search_players(conn, "queue", CURRENT_CUP)
    assert all(r["source"] == "queue" and r["account_id"] is None for r in queue)
    assert search_players(conn, "  ", CURRENT_CUP) == []
