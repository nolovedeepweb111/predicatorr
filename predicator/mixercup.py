"""Живые данные платформы mixer-cup: активный турнир, составы, игры.

Ловушки из заметки, которые здесь учтены:
- у старой копии (api.mixer-cup.gg) нет полей недель: запрос с weekId/weekNumber
  отвечает 400 и роняет всё, поэтому поля подставляются только копиям с неделями;
- Steam-аккаунта в API нет, он вытаскивается из ссылки на аватар;
- result в играх — WIN1/WIN2 относительно team1/team2.
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Any

from .config import MIXER_APIS, MixerApi
from .db import set_meta, transaction
from .http import FetchError, post_json

STEAM64_BASE = 76561197960265728
_AVATAR_RE = re.compile(r"(7656119\d{10})")
PAGE = 50


class GraphQLError(RuntimeError):
    pass


def account_from_avatar(url: str | None) -> int | None:
    if not url:
        return None
    m = _AVATAR_RE.search(url)
    return int(m.group(1)) - STEAM64_BASE if m else None


def gql(api: MixerApi, query: str, variables: dict | None = None) -> dict:
    resp = post_json(api.url, {"query": query, "variables": variables or {}}, timeout=30,
                     headers={"Origin": api.url.replace("api.", "", 1)})
    if resp.get("errors"):
        raise GraphQLError(f"{api.code}: {resp['errors'][0].get('message', resp['errors'])}")
    return resp.get("data") or {}


def active_tournament(api: MixerApi) -> dict | None:
    data = gql(api, "{ activeTournament { id name status } }")
    return data.get("activeTournament")


def active_week(api: MixerApi, tournament: int) -> dict | None:
    if not api.has_weeks:
        return None
    data = gql(api, "query($t:Int!){ tournamentWeeks(tournamentId:$t){"
                    " id weekNumber status startTime endTime } }", {"t": tournament})
    weeks = data.get("tournamentWeeks") or []
    active = [w for w in weeks if (w.get("status") or "").upper() == "ACTIVE"]
    if active:
        return active[0]
    return max(weeks, key=lambda w: w.get("weekNumber") or 0) if weeks else None


def fetch_teams(api: MixerApi, tournament: int) -> list[dict]:
    week_field = " weekId" if api.has_weeks else ""
    query = ("query($t:Int!,$f:Int!,$o:Int!){ teams(first:$f, offset:$o,"
             " filters:{tournamentId:$t}){ pageInfo { totalFiltered } items { id name number"
             + week_field + " players { id nickname proName steamAvatar rating preferredRoles }"
             " } } }")
    items: list[dict] = []
    offset = 0
    while True:
        data = gql(api, query, {"t": tournament, "f": PAGE, "o": offset})
        page = (data.get("teams") or {}).get("items") or []
        items.extend(page)
        total = ((data.get("teams") or {}).get("pageInfo") or {}).get("totalFiltered")
        offset += len(page)
        if len(page) < PAGE or (total is not None and offset >= total):
            return items


def fetch_games(api: MixerApi, tournament: int) -> list[dict]:
    week_field = " weekNumber" if api.has_weeks else ""
    fields = ("items { id status matchId result" + week_field
              + " team1 { id number name } team2 { id number name } }")
    items: list[dict] = []
    for status_filter in ("", ", status: COMPLETE"):
        query = ("query($t:Int!,$f:Int!,$o:Int!){ games(first:$f, offset:$o,"
                 " filters:{tournamentId:$t" + status_filter + "}){ " + fields + " } }")
        items, offset = [], 0
        try:
            while True:
                data = gql(api, query, {"t": tournament, "f": PAGE, "o": offset})
                page = (data.get("games") or {}).get("items") or []
                items.extend(page)
                offset += len(page)
                if len(page) < PAGE:
                    return items
        except (GraphQLError, FetchError):
            # без фильтра по статусу схема может не пустить — тогда хотя бы сыгранные
            if status_filter:
                raise
    return items


def _player_account(conn: sqlite3.Connection, p: dict, now: int) -> int:
    """Steam-аккаунт игрока или наш отрицательный id, если аватара нет."""
    acc = account_from_avatar(p.get("steamAvatar"))
    uuid = p.get("id")
    if acc is None:
        row = conn.execute("SELECT account_id FROM mixer_players WHERE mixer_uuid = ?",
                           (uuid,)).fetchone() if uuid else None
        if row:
            acc = row[0]
        else:
            low = conn.execute("SELECT MIN(account_id) FROM players").fetchone()[0] or 0
            acc = min(low, 0) - 1
    roles = ",".join(p.get("preferredRoles") or [])
    conn.execute(
        "INSERT INTO players(account_id, name, mmr, preferred_roles, team_id, roster_confirmed)"
        " VALUES (?,?,?,?,NULL,0) ON CONFLICT(account_id) DO UPDATE SET"
        " name = COALESCE(excluded.name, players.name),"
        " mmr = COALESCE(excluded.mmr, players.mmr),"
        " preferred_roles = CASE WHEN excluded.preferred_roles != '' THEN excluded.preferred_roles"
        " ELSE players.preferred_roles END",
        (acc, p.get("nickname"), p.get("rating"), roles))
    if uuid:
        conn.execute("INSERT INTO mixer_players(mixer_uuid, account_id, nickname, rating, updated_at)"
                     " VALUES (?,?,?,?,?) ON CONFLICT(mixer_uuid) DO UPDATE SET"
                     " account_id=excluded.account_id, nickname=excluded.nickname,"
                     " rating=excluded.rating, updated_at=excluded.updated_at",
                     (uuid, acc, p.get("nickname"), p.get("rating"), now))
    return int(acc)


def sync_api(conn: sqlite3.Connection, api: MixerApi) -> dict[str, Any]:
    t = active_tournament(api)
    if not t or t.get("id") is None:
        return {"series": api.code, "active": None}
    local_id = int(t["id"])
    global_id = local_id + api.offset
    status = (t.get("status") or "").upper()
    result: dict[str, Any] = {"series": api.code, "active": global_id, "name": t.get("name"),
                              "status": status}
    if status == "REDUCTION":
        return result          # идёт набор: команд ещё нет
    week = active_week(api, local_id)
    teams = fetch_teams(api, local_id)
    if week and any(tm.get("weekId") for tm in teams):
        teams = [tm for tm in teams if tm.get("weekId") == week.get("id")] or teams
    games = fetch_games(api, local_id)
    now = int(time.time())
    with transaction(conn):
        conn.execute("DELETE FROM live_teams WHERE tournament_id = ?", (global_id,))
        conn.execute("DELETE FROM live_team_players WHERE tournament_id = ?", (global_id,))
        for tm in teams:
            conn.execute("INSERT INTO live_teams(tournament_id, team_key, name, number, week_id,"
                         " synced_at) VALUES (?,?,?,?,?,?)",
                         (global_id, tm["id"], tm.get("name"), tm.get("number"),
                          tm.get("weekId"), now))
            for pos, p in enumerate(tm.get("players") or []):
                acc = _player_account(conn, p, now)
                conn.execute("INSERT OR IGNORE INTO live_team_players(tournament_id, team_key,"
                             " account_id, position) VALUES (?,?,?,?)",
                             (global_id, tm["id"], acc, pos))
        # API отдаёт игры от последней по расписанию к первой: сыгранные в конце списка.
        for i, g in enumerate(games):
            conn.execute(
                "INSERT INTO live_games(game_id, tournament_id, status, match_id, result,"
                " team1_key, team2_key, week_number, seq, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(game_id) DO UPDATE SET status=excluded.status,"
                " match_id=excluded.match_id, result=excluded.result,"
                " team1_key=excluded.team1_key, team2_key=excluded.team2_key,"
                " week_number=excluded.week_number, seq=excluded.seq, synced_at=excluded.synced_at",
                (str(g["id"]), global_id, g.get("status"),
                 int(g["matchId"]) if g.get("matchId") else None, g.get("result"),
                 (g.get("team1") or {}).get("id"), (g.get("team2") or {}).get("id"),
                 g.get("weekNumber"), len(games) - 1 - i, now))
        if t.get("name"):
            set_meta(conn, f"tournament_name_{global_id}", str(t["name"]))
        set_meta(conn, f"mixer_sync_{api.code}", str(now))
    result.update(teams=len(teams), games=len(games), week=week.get("weekNumber") if week else None)
    return result


def sync_all(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for api in MIXER_APIS:
        try:
            out.append(sync_api(conn, api))
        except (FetchError, GraphQLError, KeyError, TypeError, ValueError) as exc:
            out.append({"series": api.code, "error": str(exc)[:300]})
    return out
