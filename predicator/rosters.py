"""Текущие составы команд, ручные замены и поиск игроков."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass, field

from .config import series_of
from .db import get_meta, transaction

LIVE_FRESH_SECONDS = 6 * 3600


@dataclass
class RosterPlayer:
    account_id: int
    name: str
    mmr: float | None
    roles: list[str]
    games: int
    substitute: bool = False       # пришёл по ручной замене


@dataclass
class Team:
    key: str
    tournament_id: int
    name: str
    number: int | None
    team_ids: list[int]
    players: list[RosterPlayer]
    changes: list[dict] = field(default_factory=list)

    @property
    def lineup(self) -> list[int]:
        return [p.account_id for p in self.players]

    def as_dict(self) -> dict:
        d = asdict(self)
        d["letter"] = chr(ord("A") + self.number - 1) if self.number and 0 < self.number <= 26 else None
        return d


def tournament_title(tournament_id: int, conn: sqlite3.Connection | None = None) -> str:
    if conn is not None:
        name = get_meta(conn, f"tournament_name_{tournament_id}")
        if name:
            return name
    api = series_of(tournament_id)
    if api is None:
        return f"Турнир {tournament_id}"
    return f"{api.title} #{tournament_id - api.offset}"


def _history_games(conn: sqlite3.Connection) -> dict[int, int]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT account_id, COUNT(*) FROM match_players GROUP BY account_id")}


def tournaments(conn: sqlite3.Connection) -> list[dict]:
    """Турниры, для которых известны составы команд."""
    out: dict[int, dict] = {}
    for r in conn.execute(
            "SELECT t.tournament_id AS tid, COUNT(DISTINCT COALESCE(t.mixer_uuid, t.team_id)) AS teams,"
            " COUNT(p.account_id) AS players, SUM(p.roster_confirmed) AS confirmed"
            " FROM teams t LEFT JOIN players p ON p.team_id = t.team_id"
            " WHERE t.tournament_id IS NOT NULL GROUP BY t.tournament_id"):
        out[r["tid"]] = {"id": r["tid"], "teams": r["teams"], "players": r["players"],
                         "confirmed": r["confirmed"] or 0, "live": False}
    cutoff = int(time.time()) - LIVE_FRESH_SECONDS
    for r in conn.execute(
            "SELECT tournament_id AS tid, COUNT(*) AS teams FROM live_teams"
            " WHERE synced_at >= ? GROUP BY tournament_id", (cutoff,)):
        row = out.setdefault(r["tid"], {"id": r["tid"], "players": 0, "confirmed": 0})
        row.update(teams=r["teams"], live=True)
    games = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT tournament_id, COUNT(*), MAX(start_time) FROM matches"
        " WHERE tournament_id IS NOT NULL AND radiant_win IS NOT NULL GROUP BY tournament_id")}
    for tid, row in out.items():
        row["title"] = tournament_title(tid, conn)
        row["games"], row["last_game"] = games.get(tid, (0, None))
    return sorted(out.values(), key=lambda r: (-int(r.get("live", False)), -r["confirmed"], -r["id"]))


def default_tournament(conn: sqlite3.Connection) -> int | None:
    ts = [t for t in tournaments(conn) if t["teams"] >= 2]
    return ts[0]["id"] if ts else None


def _changes(conn: sqlite3.Connection, tournament_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT c.*, po.name AS out_name, pi.name AS in_name FROM roster_changes c"
        " LEFT JOIN players po ON po.account_id = c.out_account"
        " LEFT JOIN players pi ON pi.account_id = c.in_account"
        " WHERE c.tournament_id = ? ORDER BY c.id", (tournament_id,)).fetchall()


def load_teams(conn: sqlite3.Connection, tournament_id: int) -> list[Team]:
    players = {r["account_id"]: r for r in conn.execute("SELECT * FROM players")}
    games = _history_games(conn)

    def make(acc: int) -> RosterPlayer:
        p = players.get(acc)
        return RosterPlayer(
            account_id=acc, name=(p["name"] if p else None) or str(acc),
            mmr=p["mmr"] if p else None,
            roles=[x for x in ((p["preferred_roles"] if p else "") or "").split(",") if x],
            games=games.get(acc, 0))

    teams: dict[str, Team] = {}
    cutoff = int(time.time()) - LIVE_FRESH_SECONDS
    live = conn.execute("SELECT * FROM live_teams WHERE tournament_id = ? AND synced_at >= ?",
                        (tournament_id, cutoff)).fetchall()
    if live:
        members: dict[str, list[int]] = {}
        for r in conn.execute("SELECT * FROM live_team_players WHERE tournament_id = ?"
                              " ORDER BY position", (tournament_id,)):
            members.setdefault(r["team_key"], []).append(r["account_id"])
        steam_ids: dict[str, list[int]] = {}
        for r in conn.execute("SELECT team_id, mixer_uuid FROM teams WHERE tournament_id = ?",
                              (tournament_id,)):
            if r["mixer_uuid"]:
                steam_ids.setdefault(r["mixer_uuid"], []).append(r["team_id"])
        for r in live:
            teams[r["team_key"]] = Team(
                key=r["team_key"], tournament_id=tournament_id, name=r["name"] or r["team_key"],
                number=r["number"], team_ids=steam_ids.get(r["team_key"], []),
                players=[make(a) for a in members.get(r["team_key"], [])])
    else:
        for r in conn.execute("SELECT * FROM teams WHERE tournament_id = ? ORDER BY team_id",
                              (tournament_id,)):
            key = r["mixer_uuid"] or f"team:{r['team_id']}"
            team = teams.get(key)
            if team is None:
                team = teams[key] = Team(key=key, tournament_id=tournament_id,
                                         name=r["name"] or key, number=None, team_ids=[],
                                         players=[])
            team.team_ids.append(r["team_id"])
            team.name = r["name"] or team.name
            for p in conn.execute("SELECT account_id FROM players WHERE team_id = ?"
                                  " ORDER BY roster_confirmed DESC, account_id", (r["team_id"],)):
                if p["account_id"] not in team.lineup:
                    team.players.append(make(p["account_id"]))

    for ch in _changes(conn, tournament_id):
        team = teams.get(ch["team_key"])
        if team is None:
            continue
        lineup = team.lineup
        applied = ch["out_account"] in lineup and ch["in_account"] not in lineup
        if applied:
            idx = lineup.index(ch["out_account"])
            sub = make(ch["in_account"])
            sub.substitute = True
            team.players[idx] = sub
        team.changes.append({
            "id": ch["id"], "out_account": ch["out_account"], "in_account": ch["in_account"],
            "out_name": ch["out_name"], "in_name": ch["in_name"], "created_at": ch["created_at"],
            "note": ch["note"], "applied": applied})

    return sorted((t for t in teams.values() if t.players),
                  key=lambda t: (t.number or 999, t.name.lower()))


def find_team(conn: sqlite3.Connection, tournament_id: int, team_key: str) -> Team | None:
    return next((t for t in load_teams(conn, tournament_id) if t.key == team_key), None)


def add_change(conn: sqlite3.Connection, tournament_id: int, team_key: str, out_account: int,
               in_account: int, note: str | None = None) -> int:
    team = find_team(conn, tournament_id, team_key)
    if team is None:
        raise ValueError("команда не найдена")
    if out_account not in team.lineup:
        raise ValueError("этого игрока нет в составе")
    if in_account in team.lineup:
        raise ValueError("этот игрок уже в составе")
    if not conn.execute("SELECT 1 FROM players WHERE account_id = ?", (in_account,)).fetchone():
        raise ValueError("игрок для замены не найден")
    cur = conn.execute(
        "INSERT INTO roster_changes(tournament_id, team_key, out_account, in_account, created_at,"
        " note) VALUES (?,?,?,?,datetime('now'),?)",
        (tournament_id, team_key, out_account, in_account, note))
    return int(cur.lastrowid)


def delete_change(conn: sqlite3.Connection, change_id: int) -> bool:
    return conn.execute("DELETE FROM roster_changes WHERE id = ?", (change_id,)).rowcount > 0


def ensure_local_player(conn: sqlite3.Connection, name: str, mmr: float | None,
                        roles: str = "") -> int:
    """Игрок без Steam-привязки (например, из очереди на замену): заводим с id < 0."""
    row = conn.execute("SELECT account_id FROM players WHERE account_id < 0 AND name = ?",
                       (name,)).fetchone()
    if row:
        if mmr is not None:
            conn.execute("UPDATE players SET mmr = ? WHERE account_id = ?", (mmr, row[0]))
        return int(row[0])
    with transaction(conn):
        low = conn.execute("SELECT MIN(account_id) FROM players").fetchone()[0] or 0
        acc = min(low, 0) - 1
        conn.execute("INSERT INTO players(account_id, name, mmr, preferred_roles, team_id,"
                     " roster_confirmed) VALUES (?,?,?,?,NULL,0)", (acc, name, mmr, roles))
    return acc


def search_players(conn: sqlite3.Connection, query: str, tournament_id: int | None = None,
                   limit: int = 20) -> list[dict]:
    q = query.strip().lower()
    if not q:
        return []
    games = _history_games(conn)
    team_of: dict[int, str] = {}
    if tournament_id is not None:
        for t in load_teams(conn, tournament_id):
            for p in t.players:
                team_of[p.account_id] = t.name
    like = f"%{q}%"
    out = []
    names = set()
    for r in conn.execute("SELECT * FROM players WHERE lower(name) LIKE ?", (like,)):
        names.add((r["name"] or "").lower())
        out.append({
            "account_id": r["account_id"], "queue_uuid": None, "name": r["name"], "mmr": r["mmr"],
            "roles": [x for x in (r["preferred_roles"] or "").split(",") if x],
            "games": games.get(r["account_id"], 0), "team": team_of.get(r["account_id"]),
            "queue_position": None, "source": "player"})
    queued = {}
    for r in conn.execute("SELECT * FROM queued_players WHERE lower(nickname) LIKE ?"
                          " ORDER BY queue_position", (like,)):
        nick = (r["nickname"] or "").lower()
        hit = next((o for o in out if (o["name"] or "").lower() == nick), None)
        if hit is not None:
            if hit["queue_position"] is None:
                hit["queue_position"] = r["queue_position"]
            continue
        if nick in queued:
            continue
        queued[nick] = {
            "account_id": None, "queue_uuid": r["player_uuid"], "name": r["nickname"],
            "mmr": r["rating"], "roles": [], "games": 0, "team": None,
            "queue_position": r["queue_position"], "source": "queue"}
    out.extend(queued.values())

    def rank(o: dict) -> tuple:
        name = (o["name"] or "").lower()
        return (0 if name == q else 1 if name.startswith(q) else 2, -o["games"], name)

    return sorted(out, key=rank)[:limit]
