"""Эксперимент: виртуальные ставки по нескольким стратегиям, у каждой свой банк 5000 ₽.

Решение принимается в реальном времени по тому, что сайт знает в этот момент, и больше
не меняется: ставка записывается с коэффициентом и нашей вероятностью и закрывается по
результату из mixer-cup. Задним числом ничего не пересчитывается — поэтому сравнение
стратегий честное, даже если модель потом поменяется.

Когда ставит стратегия:
- open — по первой цене, с которой игра появилась в линии;
- close — по последней цене до начала игры (решение — когда игра началась);
- draft — после полного драфта до горна, по лайв-цене и с героями в прогнозе.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import Callable

from .db import get_meta, set_meta, transaction

START_BANK = 5000.0
MIN_STAKE = 10.0


@dataclass(frozen=True)
class Strategy:
    code: str
    name: str
    note: str
    timing: str                 # open | close | draft
    kelly: float = 0.0          # доля Келли; 0 — плоская ставка
    flat: float = 0.0           # плоская ставка, доля стартового банка
    min_edge: float = 0.03      # ставим, если ожидание на рубль не меньше
    cap: float = 0.06           # не больше этой доли свободного банка
    max_odds: float = 10.0
    rule: str = "value"         # value — сторона с лучшим ожиданием; pari_favorite — фаворит букмекера


STRATEGIES: tuple[Strategy, ...] = (
    Strategy("careful", "Осторожная", "¼ Келли, перевес от 5%, не больше 3% банка, коэффициент до 3",
             "close", kelly=0.25, min_edge=0.05, cap=0.03, max_odds=3.0),
    Strategy("moderate", "Умеренная", "½ Келли, перевес от 3%, не больше 6% банка", "close", kelly=0.5),
    Strategy("aggressive", "Агрессивная", "полный Келли, перевес от 2%, до 15% банка",
             "close", kelly=1.0, min_edge=0.02, cap=0.15),
    Strategy("flat", "Плоская", "100 ₽ на каждый перевес от 3%", "close", flat=0.02),
    Strategy("opening", "На открытии", "½ Келли по первой цене в линии", "open", kelly=0.5),
    Strategy("draft", "Окно драфта", "½ Келли после полного драфта до горна, прогноз с героями",
             "draft", kelly=0.5),
    Strategy("control", "Контроль: фаворит PARI", "100 ₽ на фаворита букмекера, без модели",
             "close", flat=0.02, min_edge=-1.0, rule="pari_favorite"),
)
BY_CODE = {s.code: s for s in STRATEGIES}


def started_at(conn: sqlite3.Connection) -> int:
    value = get_meta(conn, "paper_started_at")
    if not value:
        value = str(int(time.time()))
        set_meta(conn, "paper_started_at", value)
    return int(value)


def reset(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM paper_bets")
        conn.execute("DELETE FROM paper_decisions")
    set_meta(conn, "paper_started_at", str(int(time.time())))


# --- банк и размер ставки ---------------------------------------------------------

def bank_state(conn: sqlite3.Connection, code: str) -> dict:
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN status IN ('won','lost','void') THEN profit END), 0),"
        " COALESCE(SUM(CASE WHEN status = 'open' THEN stake END), 0) FROM paper_bets WHERE strategy = ?",
        (code,)).fetchone()
    bank = START_BANK + row[0]
    return {"bank": bank, "open": row[1], "free": bank - row[1]}


def choose(s: Strategy, p_a: float, k_a: float, k_b: float, free: float) -> dict | None:
    """Сторона и сумма по правилам стратегии; None — не ставим."""
    if s.rule == "pari_favorite":
        side = "a" if k_a <= k_b else "b"
    else:
        evs = {"a": p_a * k_a - 1, "b": (1 - p_a) * k_b - 1}
        side = max(evs, key=evs.get)
        if evs[side] < s.min_edge:
            return None
    odds = k_a if side == "a" else k_b
    p = p_a if side == "a" else 1 - p_a
    if odds > s.max_odds or odds <= 1:
        return None
    if s.kelly:
        f = max(0.0, (p * odds - 1) / (odds - 1))
        stake = min(free * s.kelly * f, free * s.cap)
    else:
        stake = START_BANK * s.flat
    stake = float(int(stake))                    # целыми рублями, вниз
    if stake < MIN_STAKE or stake > free:
        return None
    margin = 1 / k_a + 1 / k_b
    book = (1 / odds) / margin
    return {"side": side, "odds": odds, "stake": stake, "prob": p, "book_prob": book, "ev": p * odds - 1}


def place(conn: sqlite3.Connection, s: Strategy, game: dict, k_a: float, k_b: float, p_a: float,
          details: dict | None = None) -> dict | None:
    """Принять решение по игре (один раз на стратегию) и записать ставку, если она есть."""
    if conn.execute("SELECT 1 FROM paper_decisions WHERE strategy = ? AND game_id = ?",
                    (s.code, game["game_id"])).fetchone():
        return None
    pick = choose(s, p_a, k_a, k_b, bank_state(conn, s.code)["free"])
    now = int(time.time())
    with transaction(conn):
        conn.execute("INSERT INTO paper_decisions(strategy, game_id, decided_at, outcome, odds_a, odds_b,"
                     " prob_a) VALUES (?,?,?,?,?,?,?)",
                     (s.code, game["game_id"], now, "bet" if pick else "skip", k_a, k_b, p_a))
        if not pick:
            return None
        conn.execute(
            "INSERT INTO paper_bets(strategy, game_id, tournament_id, team_a_key, team_b_key, team_a, team_b,"
            " pick, odds, stake, prob, book_prob, ev, stage, placed_at, status, profit, details)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open', 0, ?)",
            (s.code, game["game_id"], game["tournament_id"], game["team_a_key"], game["team_b_key"],
             game["team_a"], game["team_b"], pick["side"], pick["odds"], pick["stake"], pick["prob"],
             pick["book_prob"], pick["ev"], s.timing, now, json.dumps(details or {}, ensure_ascii=False)))
    return pick


def settle(conn: sqlite3.Connection) -> int:
    """Закрыть ставки по результатам mixer-cup."""
    done = 0
    rows = conn.execute(
        "SELECT b.id, b.pick, b.odds, b.stake, b.team_a_key, g.result, g.team1_key, g.team2_key"
        " FROM paper_bets b JOIN live_games g ON g.game_id = b.game_id WHERE b.status = 'open'"
        " AND g.status = 'COMPLETE'").fetchall()
    now = int(time.time())
    for r in rows:
        if r["result"] not in ("WIN1", "WIN2"):
            status, profit = "void", 0.0          # игра без результата — ставка возвращается
        else:
            winner = r["team1_key"] if r["result"] == "WIN1" else r["team2_key"]
            won = (winner == r["team_a_key"]) == (r["pick"] == "a")
            status, profit = ("won", r["stake"] * (r["odds"] - 1)) if won else ("lost", -r["stake"])
        conn.execute("UPDATE paper_bets SET status = ?, profit = ?, settled_at = ? WHERE id = ?",
                     (status, round(profit, 2), now, r["id"]))
        done += 1
    return done


# --- шаг эксперимента ---------------------------------------------------------------

def _games(conn: sqlite3.Connection, tid: int) -> dict[frozenset, dict]:
    """Ещё не сыгранные игры кубка по паре команд (ближайшая, если пара встречается не раз)."""
    out: dict[frozenset, dict] = {}
    names = {r["team_key"]: r["name"] for r in conn.execute(
        "SELECT team_key, name FROM live_teams WHERE tournament_id = ?", (tid,))}
    for r in conn.execute("SELECT * FROM live_games WHERE tournament_id = ? AND status != 'COMPLETE'"
                          " ORDER BY COALESCE(planned_time, 9e18), seq", (tid,)):
        pair = frozenset((r["team1_key"], r["team2_key"]))
        if pair in out or None in pair:
            continue
        out[pair] = {"game_id": r["game_id"], "tournament_id": tid, "status": r["status"],
                     "team_a_key": r["team1_key"], "team_b_key": r["team2_key"],
                     "team_a": names.get(r["team1_key"], "?"), "team_b": names.get(r["team2_key"], "?"),
                     "start_time": r["start_time"]}
    return out


def step(conn: sqlite3.Connection, tid: int, events: list[dict], lives: list[dict],
         predict: Callable[..., float], closing: Callable[[str, str, int], tuple | None]) -> bool:
    """Один шаг: решения по линии, по цене перед началом и в окне драфта, закрытие ставок.

    closing(a, b, start) — последняя цена PARI до начала игры (k_a, k_b) или None.
    Возвращает True, если идёт игра (тогда шаги нужны чаще — окно драфта короткое).
    """
    started_at(conn)
    games = _games(conn, tid)
    events_by_pair = {frozenset((ev["team1_key"], ev["team2_key"])): ev for ev in events if ev.get("linked")}
    lives_by_pair = {frozenset(g["team_keys"].values()): g for g in lives}
    for pair, game in games.items():
        a, b = game["team_a_key"], game["team_b_key"]
        ev = events_by_pair.get(pair)
        k_a = k_b = None
        if ev:
            k_a, k_b = (ev["k1"], ev["k2"]) if ev["team1_key"] == a else (ev["k2"], ev["k1"])
        priced = bool(ev and k_a and k_b and not ev["blocked"])
        try:
            if game["status"] == "PENDING" and priced and ev["place"] == "line":
                p_a = predict(a, b)
                for s in STRATEGIES:
                    if s.timing == "open":
                        place(conn, s, game, k_a, k_b, p_a)
            if game["status"] == "ACTIVE":
                close = closing(a, b, game["start_time"] or (ev or {}).get("start_time") or int(time.time()))
                if close:
                    p_a = predict(a, b)
                    for s in STRATEGIES:
                        if s.timing == "close":
                            place(conn, s, game, close[0], close[1], p_a)
            live = lives_by_pair.get(pair)
            # PARI может держать игру и в линии, и в лайве — важна только открытая цена
            if (live and priced and live["draft_complete"] and live["assigned"] == 10
                    and live["before_horn"]):
                side_a = "radiant" if live["team_keys"]["radiant"] == a else "dire"
                side_b = "dire" if side_a == "radiant" else "radiant"
                heroes = {x["account_id"]: x["hero_id"] for x in live["heroes"]}
                la, lb = live["lineups"][side_a], live["lineups"][side_b]
                if len(set(la)) == 5 and len(set(lb)) == 5:
                    ha, hb = [heroes.get(x) for x in la], [heroes.get(x) for x in lb]
                    p_a = predict(a, b, lineup_a=la, lineup_b=lb, heroes_a=ha, heroes_b=hb)
                    for s in STRATEGIES:
                        if s.timing == "draft":
                            place(conn, s, game, k_a, k_b, p_a, {"heroes_a": ha, "heroes_b": hb})
        except ValueError:
            continue                     # состав неполный — эту игру пропускаем
    settle(conn)
    return any(g["status"] == "ACTIVE" for g in games.values())


# --- отчёт -------------------------------------------------------------------------

def report(conn: sqlite3.Connection, limit: int = 500) -> dict:
    out = []
    for s in STRATEGIES:
        rows = conn.execute("SELECT status, stake, profit, settled_at FROM paper_bets WHERE strategy = ?"
                            " ORDER BY COALESCE(settled_at, 9e18), id", (s.code,)).fetchall()
        bank, peak, drawdown = START_BANK, START_BANK, 0.0
        history = []
        for r in rows:
            if r["status"] in ("won", "lost", "void"):
                bank += r["profit"]
                peak = max(peak, bank)
                drawdown = max(drawdown, (peak - bank) / peak)
                history.append([r["settled_at"], round(bank, 2)])
        settled = [r for r in rows if r["status"] in ("won", "lost")]
        turnover = sum(r["stake"] for r in settled)
        profit = sum(r["profit"] for r in settled)
        out.append({**asdict(s), "bank": round(bank, 2), "profit": round(profit, 2),
                    "turnover": turnover, "roi": round(profit / turnover, 4) if turnover else None,
                    "bets": len(rows), "won": sum(r["status"] == "won" for r in rows),
                    "lost": sum(r["status"] == "lost" for r in rows),
                    "open": sum(r["status"] == "open" for r in rows),
                    "open_stake": sum(r["stake"] for r in rows if r["status"] == "open"),
                    "max_drawdown": round(drawdown, 4), "history": history})
    bets = [dict(r) for r in conn.execute(
        "SELECT id, strategy, game_id, team_a, team_b, pick, odds, stake, prob, book_prob, ev, stage,"
        " placed_at, status, profit, settled_at FROM paper_bets ORDER BY placed_at DESC, id DESC LIMIT ?",
        (limit,))]
    decided = conn.execute("SELECT COUNT(DISTINCT game_id) FROM paper_decisions").fetchone()[0]
    return {"started_at": started_at(conn), "start_bank": START_BANK, "strategies": out, "bets": bets,
            "games_seen": decided}
