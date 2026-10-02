from __future__ import annotations

import time

from fastapi.testclient import TestClient

from predicator import paper
from predicator.importer import bump_data_version
from predicator.paper import BY_CODE, START_BANK, choose
from predicator.rosters import load_teams
from predicator.web import create_app
from synthetic import CURRENT_CUP

NOW = int(time.time())


def test_choose_sizes_by_strategy():
    # p=0.6 на коэф. 2.0: ожидание +20%, полный Келли 0.2 банка
    assert choose(BY_CODE["moderate"], 0.6, 2.0, 1.8, 5000)["stake"] == 300      # ½ Келли = 500 → потолок 6%
    assert choose(BY_CODE["aggressive"], 0.6, 2.0, 1.8, 5000)["stake"] == 750    # 1000 → потолок 15%
    assert choose(BY_CODE["careful"], 0.6, 2.0, 1.8, 5000)["stake"] == 150       # 250 → потолок 3%
    assert choose(BY_CODE["flat"], 0.6, 2.0, 1.8, 5000)["stake"] == 100
    assert choose(BY_CODE["careful"], 0.25, 4.5, 1.2, 5000) is None               # коэф. выше 3 — мимо
    assert choose(BY_CODE["moderate"], 0.52, 1.9, 1.9, 5000) is None              # перевес меньше 3%
    fav = choose(BY_CODE["control"], 0.3, 1.5, 2.6, 5000)                          # модель против, но
    assert fav["side"] == "a" and fav["stake"] == 100                              # контроль берёт фаворита
    assert choose(BY_CODE["moderate"], 0.6, 2.0, 1.8, 5) is None                   # банк кончился


def setup_game(conn, status="PENDING", start=None, result=None):
    for key, name in (("A", "Team Alpha"), ("B", "Team Beta")):
        conn.execute("INSERT OR REPLACE INTO live_teams(tournament_id, team_key, name, number, week_id, synced_at)"
                     " VALUES (31, ?, ?, NULL, NULL, ?)", (key, name, NOW))
    conn.execute("INSERT OR REPLACE INTO live_games(game_id, tournament_id, status, match_id, result, team1_key,"
                 " team2_key, seq, planned_time, start_time, synced_at) VALUES ('g1', 31, ?, NULL, ?, 'A', 'B',"
                 " 1, ?, ?, ?)", (status, result, NOW + 600, start, NOW))


def event(place="line", k1=2.0, k2=1.8, blocked=False):
    # у PARI команды в обратном порядке: проверяем, что коэффициенты разворачиваются
    return {"linked": True, "team1_key": "B", "team2_key": "A", "k1": k2, "k2": k1, "place": place,
            "blocked": blocked, "event_id": 5, "start_time": NOW}


def predict(a, b, **kw):
    assert (a, b) == ("A", "B")
    return 0.7 if kw.get("heroes_a") else 0.6


def live_game(before_horn=True, assigned=10):
    return {"team_keys": {"radiant": "B", "dire": "A"}, "draft_complete": True, "assigned": assigned,
            "before_horn": before_horn, "lineups": {"radiant": [6, 7, 8, 9, 10], "dire": [1, 2, 3, 4, 5]},
            "heroes": [{"account_id": i, "hero_id": i + 20, "provisional": False} for i in range(1, 11)]}


def test_experiment_flow(conn):
    closing = lambda a, b, start: (1.9, 2.0)                        # noqa: E731
    setup_game(conn)
    paper.step(conn, 31, [event()], [], predict, closing)
    paper.step(conn, 31, [event()], [], predict, closing)            # второй шаг ничего не дублирует
    bets = conn.execute("SELECT strategy, pick, odds, stake, stage FROM paper_bets").fetchall()
    assert [tuple(b) for b in bets] == [("opening", "a", 2.0, 300.0, "open")]

    setup_game(conn, status="ACTIVE", start=NOW)                    # игра началась — цена перед началом
    assert paper.step(conn, 31, [event(place="live")], [live_game()], predict, closing) is True
    by = {r["strategy"]: r for r in conn.execute("SELECT * FROM paper_bets")}
    assert by["moderate"]["odds"] == 1.9 and by["moderate"]["stage"] == "close"
    assert by["control"]["pick"] == "a" and by["control"]["stake"] == 100
    assert by["draft"]["prob"] == 0.7 and by["draft"]["odds"] == 2.0               # с героями и лайв-ценой
    assert set(by) == {"opening", "careful", "moderate", "aggressive", "flat", "draft", "control"}

    setup_game(conn, status="COMPLETE", start=NOW, result="WIN1")   # выиграла A
    paper.step(conn, 31, [], [], predict, closing)
    rep = {s["code"]: s for s in paper.report(conn)["strategies"]}
    assert rep["moderate"]["won"] == 1 and rep["moderate"]["bank"] == START_BANK + 300 * 0.9
    assert rep["opening"]["bank"] == START_BANK + 300 and rep["opening"]["history"]


def test_draft_window_needs_full_assignment_before_horn(conn):
    closing = lambda a, b, start: None                              # noqa: E731
    setup_game(conn, status="ACTIVE", start=NOW)
    paper.step(conn, 31, [event(place="live")], [live_game(assigned=7)], predict, closing)
    paper.step(conn, 31, [event(place="live")], [live_game(before_horn=False)], predict, closing)
    paper.step(conn, 31, [event(place="live", blocked=True)], [live_game()], predict, closing)
    assert conn.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0] == 0


def test_paper_api(conn, settings):
    app = create_app(settings, start_sync=False)
    with TestClient(app) as client:
        rep = client.get("/api/paper").json()
        assert len(rep["strategies"]) == 7 and all(s["bank"] == START_BANK for s in rep["strategies"])
        assert client.post("/api/paper/reset").json() == {"ok": True}


def snapshot(conn, event_id, fetched, start, k1, k2, t1, t2):
    conn.execute("INSERT INTO odds_snapshots(event_id, fetched_at, team1, team2, start_time, k1, k2,"
                 " team1_key, team2_key) VALUES (?,?,?,?,?,?,?,?,?)",
                 (event_id, time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(fetched)), t1.name, t2.name,
                  start, k1, k2, t1.key, t2.key))


def test_paper_tick_uses_price_before_start(conn, settings):
    teams = load_teams(conn, CURRENT_CUP)
    a, b = teams[0], teams[1]
    start = NOW - 600
    conn.execute("INSERT INTO live_games(game_id, tournament_id, status, team1_key, team2_key, seq,"
                 " planned_time, start_time, synced_at) VALUES ('g1', ?, 'ACTIVE', ?, ?, 1, ?, ?, ?)",
                 (CURRENT_CUP, a.key, b.key, start, start, NOW))
    snapshot(conn, 7, start - 7200, start, 1.6, 2.3, b, a)      # открытие (у PARI b первой)
    snapshot(conn, 7, start - 60, start, 1.25, 4.0, b, a)       # цена перед началом: a — 4.0
    snapshot(conn, 7, start + 120, start, 1.1, 7.0, b, a)       # уже лайв — не в счёт
    snapshot(conn, 8, start - 30, start + 86400, 3.0, 1.4, a, b)  # реванш через сутки — не эта игра
    bump_data_version(conn)
    app = create_app(settings, start_sync=False)
    with TestClient(app):
        app.state.paper_tick()
    bets = conn.execute("SELECT strategy, pick, odds FROM paper_bets").fetchall()
    decided = {r[0]: r for r in conn.execute("SELECT strategy, odds_a, odds_b FROM paper_decisions")}
    assert set(decided) == {"careful", "moderate", "aggressive", "flat", "control"}
    assert all((r["odds_a"], r["odds_b"]) == (4.0, 1.25) for r in decided.values())
    assert {tuple(r) for r in bets if r["strategy"] == "control"} == {("control", "b", 1.25)}
