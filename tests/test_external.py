from __future__ import annotations

import time

from fastapi.testclient import TestClient

from predicator import external
from predicator.dataset import load_dataset
from predicator.db import get_meta
from predicator.external import ExternalData, store_matchup_week, store_player
from predicator.model import train
from predicator.web import create_app
from synthetic import HERO_IDS

WEEK = 7 * 86400


def fill_external(conn, t0: int) -> list[int]:
    """Мета и матчапы на 12 недель до t0 и история нескольких игроков."""
    weeks = [t0 - (12 - i) * WEEK for i in range(12)]
    rows = []
    for w in weeks:
        for k, h in enumerate(HERO_IDS):
            rows.append((h, w, 1000, 500 + (k - len(HERO_IDS) // 2) * 10))
    conn.executemany("INSERT INTO ext_hero_week VALUES (?,?,?,?)", rows)
    for w in weeks:
        store_matchup_week(conn, w, [(1, 2, "vs", 400, 4.0), (2, 1, "vs", 400, -4.0),
                                     (1, 5, "with", 300, 3.0), (5, 1, "with", 300, 3.0)])
    store_player(conn, 1000, {1: (300, 180), 2: (20, 8)}, {1: (60, 40)})
    store_player(conn, 1001, {5: (50, 25)}, {})
    return weeks


def test_external_features_are_time_aware(conn):
    now = int(time.time())
    weeks = fill_external(conn, now)
    ext = ExternalData.load(conn)
    assert all(ext.available().values())
    # перевес антисимметричен, синергия симметрична
    assert ext.advantage(1, 2, now) > 0 and abs(ext.advantage(1, 2, now) + ext.advantage(2, 1, now)) < 1e-9
    assert ext.synergy(1, 5, now) == ext.synergy(5, 1, now) > 0
    # до первой полной недели данных ещё нет — признаки нулевые, будущее не подсматриваем
    assert ext.advantage(1, 2, weeks[0] + 3 * 86400) == 0.0
    assert ext.hero_meta(1, weeks[0]) == 0.0
    # мета: в синтетике винрейт растёт с индексом героя
    assert ext.hero_meta(HERO_IDS[-1], now) > ext.hero_meta(HERO_IDS[0], now)


def test_external_delta_is_antisymmetric(conn):
    now = int(time.time())
    fill_external(conn, now)
    ext = ExternalData.load(conn)
    a = [(1000, 1), (1001, 5), (None, None)]
    b = [(2000, 2), (2001, 8)]
    d1, d2 = ext.delta(a, b, now), ext.delta(b, a, now)
    for k in d1:
        assert abs(d1[k] + d2[k]) < 1e-9, k
    assert d1["vs"] > 0 and d1["with"] > 0 and d1["pub_exp"] > 0


def test_player_recent_games_weigh_more(conn):
    store_player(conn, 7, {1: (100, 50), 2: (100, 50)}, {1: (100, 50)})
    ext = ExternalData.load(conn)
    stats = ext.players[7][0]
    assert stats[1].games == 100                                   # всё свежее
    assert abs(stats[2].games - 100 * external.OLD_GAMES_WEIGHT) < 1e-9


def test_train_uses_available_external_features(conn):
    fill_external(conn, int(time.time()))
    ds = load_dataset(conn)
    model = train(ds, int(time.time()), ext=ExternalData.load(conn))
    assert model.draft_features == ("hero_wr", "comfort", "meta", "vs", "with", "pub_exp")
    assert len(model.draft_coef) == 1 + len(model.draft_features)
    plain = train(ds, int(time.time()))
    assert plain.draft_features == ("hero_wr", "comfort")


def test_matchup_weeks_are_refetched_once_after_settling(conn):
    now = 1790900000                                   # пятница, 2 октября 2026
    last = now - now % WEEK - WEEK                     # четверг 24 сентября — последняя полная
    assert external.matchup_todo(conn, last - 5 * WEEK, now) == [last - i * WEEK for i in range(6)]
    store_matchup_week(conn, last, [], fetched_at=last + WEEK + 3600)       # через час после конца
    store_matchup_week(conn, last - WEEK, [], fetched_at=last + external.SETTLE + 60)
    assert external.matchup_todo(conn, last - WEEK, now) == []              # ещё не устоялась
    later = last + WEEK + external.SETTLE + 60
    assert external.matchup_todo(conn, last - WEEK, later) == [last]
    store_matchup_week(conn, last, [], fetched_at=later)
    assert external.matchup_todo(conn, last - WEEK, later + 3600) == []


def test_refresh_external_fetches_and_stores(conn, settings, monkeypatch):
    now = int(time.time())
    week = now - now % WEEK - 3 * WEEK                 # первая неделя меты

    def fake_post(url, payload, headers=None, timeout=30):
        assert headers["Authorization"] == "Bearer tok" and headers["User-Agent"] == "STRATZ_API"
        q = payload["query"]
        if "winWeek" in q:
            return {"data": {"heroStats": {"winWeek": [
                {"heroId": 1, "week": week, "matchCount": 100, "winCount": 55}]}}}
        heroes = {}
        for part in q.split("matchUp(heroId: ")[1:]:
            h = int(part.split(",")[0])
            heroes[f"h{h}"] = [{"heroId": h, "vs": [{"heroId2": 2, "matchCount": 50, "synergy": 1.5}],
                                "with": []}]
        return {"data": {"heroStats": heroes}}

    def fake_get(url, headers=None, timeout=30):
        return [{"hero_id": 1, "games": 40, "win": 22}]

    monkeypatch.setattr(external, "post_json", fake_post)
    monkeypatch.setattr(external, "get_json", fake_get)
    monkeypatch.setattr(external.time, "sleep", lambda s: None)
    from dataclasses import replace
    rep = external.refresh_external(conn, replace(settings, stratz_token="tok"))
    # матчапы — по календарю, свежие первыми, но не раньше первой недели меты
    assert rep["meta_rows"] == 1 and rep["matchup_weeks"] == [week + 2 * WEEK, week + WEEK, week]
    assert rep["players"] == external.PLAYERS_PER_RUN
    assert get_meta(conn, "external_version")
    cov = external.coverage(conn)
    assert cov["meta_weeks"] == 1 and cov["matchup_weeks"] == 3 and cov["players_public"] > 0
    assert external.refresh_external(conn, replace(settings, stratz_token="tok"))["matchup_weeks"] == []


def test_stratz_token_endpoint_never_echoes_token(conn, settings):
    app = create_app(settings, start_sync=False)
    with TestClient(app) as client:
        r = client.put("/api/settings/stratz-token", json={"token": "secret-token"})
        assert r.json() == {"stratz_token_set": True}
        status = client.get("/api/status").json()
        assert status["external"]["stratz_token_set"] is True
        assert "secret-token" not in client.get("/api/status").text
        assert client.put("/api/settings/stratz-token", json={"token": ""}).json() == {
            "stratz_token_set": False}
