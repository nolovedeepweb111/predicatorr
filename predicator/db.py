"""SQLite: схема и подключение.

История матчей повторяет бэкап донора (pari-mixer-scraper). Поверх неё живут
только наши таблицы: замены, ставки, снимки коэффициентов, настройки.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_id        INTEGER PRIMARY KEY,
    league_id       INTEGER,
    start_time      INTEGER NOT NULL,
    duration        INTEGER,
    radiant_team_id INTEGER,
    dire_team_id    INTEGER,
    radiant_win     INTEGER,          -- 1 / 0, NULL пока результата нет
    tournament_id   INTEGER           -- глобальный: PARI +0, WINLINE +20000, PARI Super +30000
);
CREATE INDEX IF NOT EXISTS matches_tournament ON matches(tournament_id, start_time);

CREATE TABLE IF NOT EXISTS match_players (
    match_id     INTEGER NOT NULL,
    account_id   INTEGER NOT NULL,
    hero_id      INTEGER,
    team_id      INTEGER,
    is_radiant   INTEGER NOT NULL,
    kills        INTEGER,
    deaths       INTEGER,
    assists      INTEGER,
    gold_per_min INTEGER,
    xp_per_min   INTEGER,
    net_worth    INTEGER,
    PRIMARY KEY (match_id, account_id)
);
CREATE INDEX IF NOT EXISTS match_players_account ON match_players(account_id);

CREATE TABLE IF NOT EXISTS match_drafts (
    match_id  INTEGER NOT NULL,
    order_num INTEGER NOT NULL,
    hero_id   INTEGER NOT NULL,
    team_id   INTEGER,
    is_pick   INTEGER NOT NULL,
    PRIMARY KEY (match_id, order_num)
);

-- Состав здесь всегда сегодняшний (правило 3 из заметки): для прошлых кубков
-- состав восстанавливается только по match_players.
CREATE TABLE IF NOT EXISTS players (
    account_id       INTEGER PRIMARY KEY,   -- < 0: игрок без Steam-привязки (заведён у нас)
    name             TEXT,
    mmr              REAL,
    preferred_roles  TEXT,
    team_id          INTEGER,
    roster_confirmed INTEGER
);

CREATE TABLE IF NOT EXISTS teams (
    team_id       INTEGER PRIMARY KEY,
    name          TEXT,
    mixer_uuid    TEXT,
    tournament_id INTEGER
);

CREATE TABLE IF NOT EXISTS queued_players (
    player_uuid    TEXT PRIMARY KEY,
    nickname       TEXT,
    rating         REAL,
    queue_position INTEGER,
    updated_at     TEXT
);

-- Живые данные mixer-cup (если сервер до него достаёт). Свежее бэкапа, поэтому
-- при наличии используются для составов вместо players.team_id.
CREATE TABLE IF NOT EXISTS live_teams (
    tournament_id INTEGER NOT NULL,
    team_key      TEXT NOT NULL,     -- uuid команды в mixer-cup
    name          TEXT,
    number        INTEGER,
    week_id       TEXT,
    synced_at     INTEGER NOT NULL,
    PRIMARY KEY (tournament_id, team_key)
);

CREATE TABLE IF NOT EXISTS live_team_players (
    tournament_id INTEGER NOT NULL,
    team_key      TEXT NOT NULL,
    account_id    INTEGER NOT NULL,
    position      INTEGER,
    PRIMARY KEY (tournament_id, team_key, account_id)
);

-- Игроки mixer-cup: uuid → Steam-аккаунт (или наш отрицательный id, если аватара нет).
CREATE TABLE IF NOT EXISTS mixer_players (
    mixer_uuid TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL,
    nickname   TEXT,
    rating     REAL,
    updated_at INTEGER
);

-- Расписание и результаты игр из mixer-cup: связывает команды с матчами Dota.
CREATE TABLE IF NOT EXISTS live_games (
    game_id       TEXT PRIMARY KEY,
    tournament_id INTEGER NOT NULL,
    status        TEXT,
    match_id      INTEGER,
    result        TEXT,              -- WIN1 / WIN2 относительно team1 / team2
    team1_key     TEXT,
    team2_key     TEXT,
    week_number   INTEGER,
    synced_at     INTEGER NOT NULL
);

-- Ручные замены. Применяются к составу из источника по порядку и только если
-- имеют смысл (ушедший ещё в составе, пришедший ещё не в нём), поэтому когда
-- источник сам узнает о замене, запись тихо становится пустой.
CREATE TABLE IF NOT EXISTS roster_changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL,
    team_key      TEXT NOT NULL,
    out_account   INTEGER NOT NULL,
    in_account    INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS bets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    tournament_id INTEGER,
    team_a_key    TEXT,
    team_b_key    TEXT,
    team_a        TEXT NOT NULL,
    team_b        TEXT NOT NULL,
    pick          TEXT NOT NULL CHECK (pick IN ('A', 'B')),
    odds          REAL NOT NULL,
    stake         REAL NOT NULL,
    model_prob    REAL,             -- наша вероятность выбранного исхода
    book_prob     REAL,             -- вероятность букмекера без маржи
    stage         TEXT,             -- prematch | draft
    pari_event_id INTEGER,
    match_id      INTEGER,
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'won', 'lost', 'void')),
    settled_at    TEXT,
    note          TEXT,
    snapshot      TEXT              -- JSON: составы и герои, по которым считался прогноз
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    event_id   INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    team1      TEXT,
    team2      TEXT,
    start_time INTEGER,
    k1         REAL,
    k2         REAL,
    PRIMARY KEY (event_id, fetched_at)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: автокоммит, многошаговые записи идут через transaction().
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
