"""Импорт публичного бэкапа донора (backup.json) в нашу базу.

Бэкап — полный снимок, поэтому история (матчи, игроки, драфты) просто
перезаписывается построчно. Наши собственные таблицы (замены, ставки) не трогаем,
как и игроков с отрицательным account_id: их заводим мы сами.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .db import get_meta, set_meta, transaction
from .http import get_bytes


def load_backup_raw(source: str | Path) -> bytes:
    source = str(source)
    if source.startswith(("http://", "https://")):
        return get_bytes(source, timeout=120)
    return Path(source).read_bytes()


def load_backup(source: str | Path) -> dict[str, Any]:
    return json.loads(load_backup_raw(source))


def refresh_backup(conn: sqlite3.Connection, source: str | Path, force: bool = False) -> dict:
    """Скачать бэкап и импортировать, если он изменился с прошлого раза."""
    raw = load_backup_raw(source)
    digest = hashlib.sha256(raw).hexdigest()
    if not force and digest == get_meta(conn, "backup_sha256"):
        return {"changed": False}
    stats = import_backup(conn, json.loads(raw))
    set_meta(conn, "backup_sha256", digest)
    bump_data_version(conn)
    return {"changed": True, **stats}


def bump_data_version(conn: sqlite3.Connection) -> None:
    set_meta(conn, "data_version", str(time.time_ns()))


def import_backup(conn: sqlite3.Connection, backup: dict[str, Any]) -> dict[str, int]:
    matches = backup.get("matches") or []
    match_players = backup.get("match_players") or {}
    drafts = backup.get("match_drafts") or {}
    players = backup.get("players") or []
    teams = backup.get("teams") or []
    queued = backup.get("queued_players") or []

    with transaction(conn):
        conn.executemany(
            "INSERT INTO matches(match_id, league_id, start_time, duration, radiant_team_id,"
            " dire_team_id, radiant_win, tournament_id) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(match_id) DO UPDATE SET league_id=excluded.league_id,"
            " start_time=excluded.start_time, duration=excluded.duration,"
            " radiant_team_id=excluded.radiant_team_id, dire_team_id=excluded.dire_team_id,"
            " radiant_win=COALESCE(excluded.radiant_win, matches.radiant_win),"
            " tournament_id=COALESCE(excluded.tournament_id, matches.tournament_id)",
            [(m[0], m[1], m[2], m[3], m[4], m[5],
              None if m[6] is None else int(bool(m[6])), m[7]) for m in matches])

        rows = []
        for mid, plist in match_players.items():
            for p in plist:
                rows.append((int(mid), p[0], p[1], p[2], int(bool(p[3])), *p[4:10]))
        conn.executemany("DELETE FROM match_players WHERE match_id = ?",
                         [(int(mid),) for mid in match_players])
        conn.executemany(
            "INSERT OR REPLACE INTO match_players(match_id, account_id, hero_id, team_id,"
            " is_radiant, kills, deaths, assists, gold_per_min, xp_per_min, net_worth)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)

        drows = []
        for mid, dlist in drafts.items():
            for d in dlist:
                drows.append((int(mid), d[0], d[1], d[2], int(bool(d[3]))))
        conn.executemany("DELETE FROM match_drafts WHERE match_id = ?",
                         [(int(mid),) for mid in drafts])
        conn.executemany(
            "INSERT OR REPLACE INTO match_drafts(match_id, order_num, hero_id, team_id, is_pick)"
            " VALUES (?,?,?,?,?)", drows)

        conn.executemany(
            "INSERT INTO players(account_id, name, mmr, preferred_roles, team_id, roster_confirmed)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET name=excluded.name,"
            " mmr=COALESCE(excluded.mmr, players.mmr), preferred_roles=excluded.preferred_roles,"
            " team_id=excluded.team_id, roster_confirmed=excluded.roster_confirmed",
            [(p["account_id"], p.get("name"), p.get("mmr"), p.get("preferred_roles") or "",
              p.get("team_id"), int(bool(p.get("roster_confirmed")))) for p in players])
        # Игрок, пропавший из бэкапа, больше ни в какой команде не числится.
        known = {p["account_id"] for p in players}
        stale = [(a,) for (a,) in conn.execute(
            "SELECT account_id FROM players WHERE account_id > 0 AND team_id IS NOT NULL")
            if a not in known]
        conn.executemany("UPDATE players SET team_id = NULL WHERE account_id = ?", stale)

        conn.execute("DELETE FROM teams")
        conn.executemany(
            "INSERT OR REPLACE INTO teams(team_id, name, mixer_uuid, tournament_id) VALUES (?,?,?,?)",
            [(t["team_id"], t.get("name"), t.get("mixer_uuid"), t.get("tournament_id"))
             for t in teams])

        if queued:
            conn.execute("DELETE FROM queued_players")
            conn.executemany(
                "INSERT OR REPLACE INTO queued_players(player_uuid, nickname, rating,"
                " queue_position, updated_at) VALUES (?,?,?,?,?)",
                [(q["player_uuid"], q.get("nickname"), q.get("rating"), q.get("queue_position"),
                  q.get("updated_at")) for q in queued])

        set_meta(conn, "backup_imported_at", str(int(time.time())))

    return {"matches": len(matches), "match_players": len(rows), "drafts": len(drows),
            "players": len(players), "teams": len(teams), "queued": len(queued)}
