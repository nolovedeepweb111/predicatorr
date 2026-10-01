from __future__ import annotations

import json

from fastapi.testclient import TestClient

from predicator import mixercup
from predicator.config import MIXER_APIS
from predicator.importer import refresh_backup
from predicator.rosters import load_teams
from predicator.web import create_app
from synthetic import CURRENT_CUP


def test_refresh_backup_detects_changes(conn, tmp_path, backup):
    path = tmp_path / "backup.json"
    path.write_text(json.dumps({k: v for k, v in backup.items() if not k.startswith("_")}))
    assert refresh_backup(conn, path)["changed"] is True
    assert refresh_backup(conn, path)["changed"] is False


def test_avatar_to_account():
    url = "https://avatars.steamstatic.com/76561199130942974_full.jpg"
    assert mixercup.account_from_avatar(url) == 76561199130942974 - 76561197960265728
    assert mixercup.account_from_avatar(None) is None


def test_mixer_sync_writes_live_rosters(conn, monkeypatch):
    sent = []

    def fake_post(url, payload, headers=None, timeout=30):
        q = payload["query"]
        sent.append((url, q))
        if "activeTournament" in q:
            return {"data": {"activeTournament": {"id": 41, "name": "Cup", "status": "ACTIVE"}}}
        if "teams(" in q:
            return {"data": {"teams": {"pageInfo": {"totalFiltered": 1}, "items": [
                {"id": "live-1", "name": "Team Live", "number": 3, "players": [
                    {"id": "p1", "nickname": "player0", "steamAvatar": "/avatars/76561197960266728.jpg",
                     "rating": 7000, "preferredRoles": ["CARRY"]},
                    {"id": "p2", "nickname": "без аватара", "steamAvatar": None, "rating": 5000,
                     "preferredRoles": []}]}]}}}
        if "games(" in q:
            return {"data": {"games": {"items": [
                {"id": "g1", "status": "COMPLETE", "matchId": "123", "result": "WIN1",
                 "team1": {"id": "live-1"}, "team2": {"id": "x"}}]}}}
        raise AssertionError(q)

    monkeypatch.setattr(mixercup, "post_json", fake_post)
    api = MIXER_APIS[0]                         # старая копия: полей недель быть не должно
    res = mixercup.sync_api(conn, api)
    assert res["active"] == 41 and res["teams"] == 1 and res["games"] == 1
    assert not any("week" in q.lower() for _, q in sent)
    teams = load_teams(conn, 41)
    assert teams[0].name == "Team Live" and teams[0].number == 3
    assert teams[0].lineup[0] == 1000 and teams[0].lineup[1] < 0
    # повторная синхронизация не плодит новых игроков без аватара
    mixercup.sync_api(conn, api)
    assert load_teams(conn, 41)[0].lineup[1] == teams[0].lineup[1]


def test_api_end_to_end(conn, settings):
    app = create_app(settings, start_sync=False)
    with TestClient(app) as client:
        st = client.get("/api/status").json()
        assert st["model"]["ready"] and st["default_tournament"] == CURRENT_CUP
        teams = client.get("/api/teams").json()["teams"]
        assert len(teams) == 8 and all("rating" in t for t in teams)
        a, b = teams[0]["key"], teams[1]["key"]
        res = client.post("/api/predict", json={"tournament_id": CURRENT_CUP, "team_a": a, "team_b": b,
                                                "heroes_a": [1, 2], "k_a": 1.9, "k_b": 1.9}).json()
        assert 0 < res["p_a"] < 1 and res["offer"]["margin"] > 0
        assert client.post("/api/predict", json={"tournament_id": CURRENT_CUP, "team_a": a,
                                                 "team_b": a}).status_code == 400
        out = teams[0]["players"][0]["account_id"]
        r = client.post("/api/rosters/change", json={"tournament_id": CURRENT_CUP, "team_key": a,
                                                     "out_account": out, "in_name": "queue1",
                                                     "in_mmr": 5100})
        assert r.status_code == 200
        assert client.delete(f"/api/rosters/change/{r.json()['id']}").json()["ok"]
        odds = client.get("/api/odds").json()
        assert odds["error"] and odds["events"] == []       # линия недоступна — честная ошибка
        bet = client.post("/api/bets", json={"team_a": "A", "team_b": "B", "pick": "B", "odds": 2.5,
                                             "stake": 100}).json()
        assert client.patch(f"/api/bets/{bet['id']}", json={"status": "won"}).json()["ok"]
        assert client.get("/api/bets").json()["summary"]["profit"] == 150.0
        assert client.put("/api/settings", json={"bankroll": 2000}).json()["bankroll"] == 2000
        assert client.get("/api/heroes").json()["heroes"][0]["name"] == "Anti-Mage"
        assert client.get("/").status_code == 200


def test_password_protection(conn, settings):
    from dataclasses import replace
    app = create_app(replace(settings, password="secret"), start_sync=False)
    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/status", auth=("me", "secret")).status_code == 200
