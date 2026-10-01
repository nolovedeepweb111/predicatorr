"""Математика ставок и журнал.

Вероятность букмекера получается из коэффициентов снятием маржи
(пропорционально). Ставка «ценная», если наша вероятность × коэффициент > 1
с запасом. Размер ставки — доля Келли с потолком от банка.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .db import get_setting, set_setting

DEFAULTS = {
    "bankroll": 10000.0,
    "kelly_fraction": 0.25,     # четверть Келли: модель ошибается, полный Келли слишком рискован
    "max_stake_pct": 0.05,      # не больше 5% банка на одну игру
    "min_edge": 0.03,           # ставку предлагаем, если ожидание выше 3% на рубль
}


def get_bet_settings(conn: sqlite3.Connection) -> dict[str, float]:
    return {k: float(get_setting(conn, k, str(v)) or v) for k, v in DEFAULTS.items()}


def save_bet_settings(conn: sqlite3.Connection, values: dict[str, float]) -> dict[str, float]:
    for k, v in values.items():
        if k in DEFAULTS and v is not None:
            set_setting(conn, k, str(float(v)))
    return get_bet_settings(conn)


@dataclass
class Offer:
    """Разбор двухисходного рынка против нашей вероятности."""

    k_a: float
    k_b: float
    p_a: float                 # наша вероятность победы A

    @property
    def margin(self) -> float:
        return 1 / self.k_a + 1 / self.k_b - 1

    @property
    def book_a(self) -> float:
        inv_a, inv_b = 1 / self.k_a, 1 / self.k_b
        return inv_a / (inv_a + inv_b)

    def ev(self, side: str) -> float:
        """Ожидаемая прибыль на рубль ставки."""
        return self.p_a * self.k_a - 1 if side == "a" else (1 - self.p_a) * self.k_b - 1

    def kelly(self, side: str) -> float:
        k = self.k_a if side == "a" else self.k_b
        p = self.p_a if side == "a" else 1 - self.p_a
        if k <= 1:
            return 0.0
        return max(0.0, (p * k - 1) / (k - 1))

    def analyse(self, settings: dict[str, float]) -> dict:
        out = {"margin": round(self.margin, 4), "book_p_a": round(self.book_a, 4),
               "model_p_a": round(self.p_a, 4), "sides": {}}
        best = None
        for side, k in (("a", self.k_a), ("b", self.k_b)):
            ev = self.ev(side)
            full = self.kelly(side)
            stake_frac = min(full * settings["kelly_fraction"], settings["max_stake_pct"])
            stake = float(round(settings["bankroll"] * stake_frac))   # ставят целыми рублями
            p = self.p_a if side == "a" else 1 - self.p_a
            info = {
                "odds": k, "model_p": round(p, 4),
                "book_p": round(self.book_a if side == "a" else 1 - self.book_a, 4),
                "fair_odds": round(1 / p, 3) if p > 0 else None,
                "min_odds": round((1 + settings["min_edge"]) / p, 3) if p > 0 else None,
                "ev": round(ev, 4), "kelly": round(full, 4),
                "stake": stake if ev >= settings["min_edge"] else 0.0,
                "value": ev >= settings["min_edge"],
            }
            out["sides"][side] = info
            if info["value"] and (best is None or ev > out["sides"][best]["ev"]):
                best = side
        out["best"] = best
        return out


# --- журнал ------------------------------------------------------------------

def add_bet(conn: sqlite3.Connection, bet: dict) -> int:
    cur = conn.execute(
        "INSERT INTO bets(created_at, tournament_id, team_a_key, team_b_key, team_a, team_b, pick,"
        " odds, stake, model_prob, book_prob, stage, pari_event_id, note, snapshot)"
        " VALUES (datetime('now'),?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (bet.get("tournament_id"), bet.get("team_a_key"), bet.get("team_b_key"), bet["team_a"],
         bet["team_b"], bet["pick"], float(bet["odds"]), float(bet["stake"]),
         bet.get("model_prob"), bet.get("book_prob"), bet.get("stage"), bet.get("pari_event_id"),
         bet.get("note"), json.dumps(bet.get("snapshot") or {}, ensure_ascii=False)))
    return int(cur.lastrowid)


def list_bets(conn: sqlite3.Connection, limit: int = 300) -> list[dict]:
    rows = conn.execute("SELECT * FROM bets ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["snapshot"] = json.loads(d["snapshot"] or "{}")
        d["profit"] = bet_profit(d)
        out.append(d)
    return out


def bet_profit(b: dict) -> float | None:
    if b["status"] == "won":
        return round(b["stake"] * (b["odds"] - 1), 2)
    if b["status"] == "lost":
        return -b["stake"]
    if b["status"] == "void":
        return 0.0
    return None


def settle_bet(conn: sqlite3.Connection, bet_id: int, status: str,
               match_id: int | None = None) -> bool:
    if status not in ("open", "won", "lost", "void"):
        raise ValueError("неизвестный статус ставки")
    return conn.execute(
        "UPDATE bets SET status = ?, match_id = COALESCE(?, match_id),"
        " settled_at = CASE WHEN ? = 'open' THEN NULL ELSE datetime('now') END WHERE id = ?",
        (status, match_id, status, bet_id)).rowcount > 0


def delete_bet(conn: sqlite3.Connection, bet_id: int) -> bool:
    return conn.execute("DELETE FROM bets WHERE id = ?", (bet_id,)).rowcount > 0


def bet_summary(conn: sqlite3.Connection) -> dict:
    bets = list_bets(conn, limit=100000)
    settled = [b for b in bets if b["status"] in ("won", "lost")]
    staked = sum(b["stake"] for b in settled)
    profit = sum(b["profit"] for b in settled)
    expected = sum(b["stake"] * ((b["model_prob"] or 0) * b["odds"] - 1)
                   for b in settled if b["model_prob"] is not None)
    return {
        "total": len(bets), "open": sum(1 for b in bets if b["status"] == "open"),
        "settled": len(settled), "won": sum(1 for b in settled if b["status"] == "won"),
        "staked": round(staked, 2), "profit": round(profit, 2),
        "roi": round(profit / staked, 4) if staked else None,
        "expected_profit": round(expected, 2),
        "open_stake": round(sum(b["stake"] for b in bets if b["status"] == "open"), 2),
    }


def auto_settle(conn: sqlite3.Connection) -> int:
    """Закрыть открытые ставки по результатам матчей.

    Матч ищется по составам из снимка ставки: не меньше трёх человек каждой
    команды на противоположных сторонах. Подходит первая игра, которая ещё шла
    в момент ставки или началась после неё (ставка по ходу игры тоже бывает),
    поэтому в серии из трёх игр прошлые игры серии не перепутаются со ставкой.
    """
    settled = 0
    for b in conn.execute("SELECT * FROM bets WHERE status = 'open'").fetchall():
        snap = json.loads(b["snapshot"] or "{}")
        lineup_a, lineup_b = set(snap.get("lineup_a") or []), set(snap.get("lineup_b") or [])
        if len(lineup_a) < 3 or len(lineup_b) < 3:
            continue
        rows = conn.execute(
            "SELECT m.match_id, m.radiant_win, mp.account_id, mp.is_radiant FROM matches m"
            " JOIN match_players mp ON mp.match_id = m.match_id"
            " WHERE m.radiant_win IS NOT NULL"
            " AND m.start_time >= CAST(strftime('%s', ?1) AS INTEGER) - 3 * 3600"
            " AND m.start_time + COALESCE(m.duration, 0) >= CAST(strftime('%s', ?1) AS INTEGER)"
            " ORDER BY m.start_time, m.match_id", (b["created_at"],)).fetchall()
        by_match: dict[int, dict] = {}
        for r in rows:
            m = by_match.setdefault(r["match_id"], {"win": r["radiant_win"], True: set(), False: set()})
            m[bool(r["is_radiant"])].add(r["account_id"])
        for match_id, m in by_match.items():
            for a_radiant in (True, False):
                if (len(m[a_radiant] & lineup_a) >= 3 and len(m[not a_radiant] & lineup_b) >= 3):
                    a_won = bool(m["win"]) == a_radiant
                    won = a_won if b["pick"] == "A" else not a_won
                    settle_bet(conn, b["id"], "won" if won else "lost", match_id)
                    settled += 1
                    break
            else:
                continue
            break
    return settled
