from __future__ import annotations

import json
import time

from predicator.bets import DEFAULTS, Offer, add_bet, auto_settle, bet_summary, list_bets
from predicator.pari import PariLine, best_team, link_events, normalize_team, team_letter


def test_offer_math():
    o = Offer(k_a=1.80, k_b=2.10, p_a=0.6)
    assert abs(o.margin - (1 / 1.8 + 1 / 2.1 - 1)) < 1e-12
    assert abs(o.book_a + (1 - o.book_a) - 1) < 1e-12
    assert abs(o.ev("a") - (0.6 * 1.8 - 1)) < 1e-12
    assert abs(o.kelly("a") - (0.6 * 1.8 - 1) / 0.8) < 1e-12
    assert o.kelly("b") == 0.0
    res = o.analyse(DEFAULTS)
    assert res["best"] == "a" and res["sides"]["a"]["value"]
    assert res["sides"]["a"]["stake"] == round(DEFAULTS["bankroll"] * min(
        o.kelly("a") * DEFAULTS["kelly_fraction"], DEFAULTS["max_stake_pct"]))
    assert res["sides"]["b"]["stake"] == 0.0


def test_no_value_suggested_in_play():
    # игра идёт: коэффициенты уже учли её ход, прогноз до начала — нет
    res = Offer(k_a=2.43, k_b=1.54, p_a=0.58).analyse(DEFAULTS, in_play=True)
    assert res["in_play"] and res["best"] is None
    assert res["sides"]["a"]["ev"] > 0.3 and not res["sides"]["a"]["value"]
    assert res["sides"]["a"]["stake"] == 0.0


def test_no_value_when_edge_below_threshold():
    res = Offer(k_a=1.90, k_b=1.90, p_a=0.53).analyse(DEFAULTS)   # ev = +0.7%
    assert res["best"] is None and not res["sides"]["a"]["value"]


PACKET = {
    "packetVersion": 10,
    "sports": [{"id": 40, "kind": "sport", "name": "Киберспорт"},
               {"id": 41, "parentId": 40, "kind": "segment", "name": "Dota 2. PARI Mixer Cup"},
               {"id": 42, "parentId": 40, "kind": "segment", "name": "CS2. Major"},
               {"id": 1, "kind": "sport", "name": "Футбол"}],
    "events": [
        {"id": 5, "sportId": 41, "level": 1, "team1": "Team ПОДПИВАСНИK", "team2": "Команда B",
         "startTime": int(time.time()) + 600, "place": "line"},
        {"id": 6, "parentId": 5, "sportId": 41, "level": 2, "name": "1-я карта"},
        {"id": 7, "sportId": 42, "level": 1, "team1": "NaVi", "team2": "G2", "startTime": int(time.time())},
        {"id": 8, "sportId": 41, "level": 1, "team1": "old", "team2": "game", "startTime": 1000},
    ],
    "customFactors": [
        {"e": 5, "factors": [{"f": 921, "v": 1.7}, {"f": 923, "v": 2.05}, {"f": 924, "v": 9}]},
        {"e": 6, "factors": [{"f": 921, "v": 1.75}, {"f": 923, "v": 2.0}]},
        {"e": 7, "factors": [{"f": 921, "v": 1.5}, {"f": 923, "v": 2.5}]},
    ],
    "eventBlocks": [{"eventId": 6, "state": "blocked"}],
}


def test_pari_packet_parsing_and_delta():
    line = PariLine(["http://x"])
    line._apply(json.loads(json.dumps(PACKET)))
    events = line.dota_events()
    assert [e.event_id for e in events] == [5]           # CS2 отброшен, старая игра вычищена
    ev = events[0]
    assert (ev.k1, ev.k2, ev.mixer) == (1.7, 2.05, True)
    assert ev.maps[0].blocked and ev.maps[0].k1 == 1.75
    line._apply({"packetVersion": 11, "customFactors": [{"e": 5, "factors": [{"f": 923, "v": 2.2}]}]})
    ev = line.dota_events()[0]
    assert (ev.k1, ev.k2) == (1.7, 2.2) and line.version == 11


def test_team_name_matching():
    assert normalize_team("Team ПОДПИВАСНИK") == normalize_team("подпивасник")
    assert normalize_team("Zvёzd") == normalize_team("Zvezd")
    assert team_letter("Команда В") == 2 and team_letter("Team Axe") is None
    teams = {"k1": "Team ПОДПИВАСНИK", "k2": "Team afka"}
    assert best_team("ПОДПИВАСНИК", teams) == ("k1", 1.0)
    assert best_team("Tundra", teams)[0] is None
    line = PariLine(["http://x"])
    line._apply(json.loads(json.dumps(PACKET)))
    linked = link_events(line.dota_events(), teams, numbers={"k1": 1, "k2": 2})
    assert linked[0]["linked"] and (linked[0]["team1_key"], linked[0]["team2_key"]) == ("k1", "k2")


def test_auto_settle_by_lineups(conn):
    m = conn.execute("SELECT * FROM matches WHERE tournament_id = 28 ORDER BY start_time LIMIT 1").fetchone()
    rows = conn.execute("SELECT account_id, is_radiant FROM match_players WHERE match_id = ?",
                        (m["match_id"],)).fetchall()
    radiant = [r[0] for r in rows if r[1]]
    dire = [r[0] for r in rows if not r[1]]
    created = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(m["start_time"] + 60))
    bet_id = add_bet(conn, {"team_a": "A", "team_b": "B", "pick": "A", "odds": 2.0, "stake": 100,
                            "model_prob": 0.55, "snapshot": {"lineup_a": radiant, "lineup_b": dire}})
    conn.execute("UPDATE bets SET created_at = ? WHERE id = ?", (created, bet_id))
    # ставка, сделанная уже после конца игры, этот матч не берёт
    late = add_bet(conn, {"team_a": "A", "team_b": "B", "pick": "A", "odds": 2.0, "stake": 100,
                          "snapshot": {"lineup_a": radiant, "lineup_b": dire}})
    conn.execute("UPDATE bets SET created_at = ? WHERE id = ?",
                 (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(m["start_time"] + m["duration"] + 600)), late))
    assert auto_settle(conn) == 1
    bets = {b["id"]: b for b in list_bets(conn)}
    assert bets[bet_id]["status"] == ("won" if m["radiant_win"] else "lost")
    assert bets[bet_id]["match_id"] == m["match_id"]
    assert bets[late]["status"] == "open"
    s = bet_summary(conn)
    assert s["settled"] == 1 and s["open"] == 1
