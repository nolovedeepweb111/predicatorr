"""Внешние данные для драфта.

STRATZ (бесплатный ключ): мета героев по неделям и матрица матчапов/синергий на
рейтинге Divine+Immortal — миллионы игр против двух тысяч в лиге. OpenDota (без
ключа): сколько и с каким винрейтом человек играл на каждом герое в рейтинговых
играх, за всю карьеру и за последние полгода — сотни игр против единиц в лиге.
Игры лиги туда не попадают (lobby_type=7).

Один ключ STRATZ работает не больше чем с двух IP за 15 минут, поэтому всё
качается только с сервера и понемногу: мета раз в сутки, матчапы — по неделе за
раз, история игрока — раз в неделю на человека.

Признаки считаются на момент матча: для истории берутся только полные недели до
него, для живого прогноза — последние недели.
"""

from __future__ import annotations

import bisect
import logging
import math
import sqlite3
import threading
import time
from array import array
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Iterable

from .config import Settings
from .db import get_meta, get_setting, set_meta, transaction
from .heroes import hero_by_id
from .http import FetchError, get_json, post_json

log = logging.getLogger("predicator.external")

STRATZ_URL = "https://api.stratz.com/graphql"
OPENDOTA_URL = "https://api.opendota.com/api"
WEEK = 7 * 86400
META_WEEKS = 30                  # сколько недель меты брать за раз
MATCHUP_WEEKS_PER_RUN = 3        # сколько недель матчапов докачивать за проход
SETTLE = 2 * 86400               # STRATZ дописывает игры недели с опозданием
PLAYER_TTL = 7 * 86400
PLAYERS_PER_RUN = 30             # OpenDota без ключа: около 60 запросов в минуту, на игрока — два
RECENT_DAYS = 180

LOOKBACK_WEEKS = 8               # окно меты и матчапов; проверено: 4–20 недель дают одно и то же
OLD_GAMES_WEIGHT = 0.3           # вес рейтинговых игр старше полугода
PUB_SHRINK = 30                  # сглаживание винрейта на герое к общему винрейту игрока
SIZE = 192                       # id героев меньше этого числа (сейчас максимум 155)


class ExternalError(RuntimeError):
    pass


def stratz_token(conn: sqlite3.Connection, settings: Settings) -> str:
    return (settings.stratz_token or get_setting(conn, "stratz_token") or "").strip()


def _stratz(token: str, query: str) -> dict:
    try:
        data = post_json(STRATZ_URL, {"query": query}, timeout=120, headers={
            "Authorization": f"Bearer {token}", "User-Agent": "STRATZ_API"})
    except FetchError as exc:
        raise ExternalError(f"STRATZ: {exc}") from exc
    if data.get("errors"):
        raise ExternalError(f"STRATZ: {data['errors'][0].get('message', data['errors'][0])}")
    return data.get("data") or {}


# --- мета ---------------------------------------------------------------------

def refresh_meta(conn: sqlite3.Connection, token: str) -> int:
    data = _stratz(token, "{ heroStats { winWeek(take: %d, bracketIds: [DIVINE, IMMORTAL],"
                          " gameModeIds: [ALL_PICK_RANKED]) { heroId week matchCount winCount } } }"
                   % META_WEEKS)
    rows = (data.get("heroStats") or {}).get("winWeek") or []
    with transaction(conn):
        conn.executemany(
            "INSERT INTO ext_hero_week(hero_id, week, matches, wins) VALUES (?,?,?,?)"
            " ON CONFLICT(hero_id, week) DO UPDATE SET matches=excluded.matches, wins=excluded.wins",
            [(r["heroId"], r["week"], r["matchCount"] or 0, r["winCount"] or 0) for r in rows
             if r.get("heroId") and r.get("week")])
    return len(rows)


# --- матчапы ------------------------------------------------------------------

def matchup_arrays(rows: Iterable[tuple[int, int, str, int, float]]) -> dict[str, tuple[array, array]]:
    """(герой, соперник/союзник, vs|with, игр, перевес%) → по виду: массивы игр и игр×перевес."""
    out = {kind: (array("f", bytes(4 * SIZE * SIZE)), array("f", bytes(4 * SIZE * SIZE)))
           for kind in ("vs", "with")}
    for a, b, kind, n, syn in rows:
        if a < SIZE and b < SIZE:
            nn, ss = out[kind]
            nn[a * SIZE + b] += n
            ss[a * SIZE + b] += n * syn
    return out


def _fetch_matchup_week(token: str, week: int) -> list[tuple[int, int, str, int, float]]:
    fields = "heroId vs { heroId2 matchCount synergy } with { heroId2 matchCount synergy }"
    ids = sorted(hero_by_id())
    out: list[tuple[int, int, str, int, float]] = []
    for i in range(0, len(ids), 10):
        chunk = ids[i:i + 10]
        query = "{ heroStats { " + " ".join(
            f"h{h}: matchUp(heroId: {h}, week: {week}, bracketBasicIds: [DIVINE_IMMORTAL], take: 200)"
            f" {{ {fields} }}" for h in chunk) + " } }"
        data = _stratz(token, query).get("heroStats") or {}
        for h in chunk:
            for row in data.get(f"h{h}") or []:
                for kind in ("vs", "with"):
                    for r in row.get(kind) or []:
                        if r.get("heroId2") and r.get("matchCount"):
                            out.append((h, r["heroId2"], kind, int(r["matchCount"]),
                                        float(r.get("synergy") or 0.0)))
        time.sleep(0.5)
    return out


def store_matchup_week(conn: sqlite3.Connection, week: int,
                       rows: Iterable[tuple[int, int, str, int, float]],
                       fetched_at: int | None = None) -> None:
    arrays = matchup_arrays(rows)
    with transaction(conn):
        for kind, (n, s) in arrays.items():
            conn.execute("INSERT OR REPLACE INTO ext_matchup_week(week, kind, n, s, fetched_at)"
                         " VALUES (?,?,?,?,?)", (week, kind, n.tobytes(), s.tobytes(),
                                                 fetched_at or int(time.time())))


def matchup_todo(conn: sqlite3.Connection, oldest: int, now: int) -> list[int]:
    """Полные недели от oldest до последней, которых нет (свежие — первыми).

    Неделю, скачанную в первые двое суток после её конца, перекачиваем ещё раз,
    когда они пройдут: STRATZ дописывает игры с опозданием.
    """
    last = now - now % WEEK - WEEK          # недели STRATZ начинаются в четверг 00:00 UTC
    have = dict(conn.execute("SELECT week, MIN(fetched_at) FROM ext_matchup_week GROUP BY week"))
    return [w for w in range(last, oldest - oldest % WEEK - 1, -WEEK)
            if w not in have or have[w] < w + WEEK + SETTLE <= now]


def refresh_matchups(conn: sqlite3.Connection, token: str, oldest: int) -> list[int]:
    done = []
    for week in matchup_todo(conn, oldest, int(time.time()))[:MATCHUP_WEEKS_PER_RUN]:
        store_matchup_week(conn, week, _fetch_matchup_week(token, week))
        done.append(week)
    return done


# --- история игроков ------------------------------------------------------------

def _player_heroes(account_id: int, days: int | None) -> dict[int, tuple[int, int]]:
    q = f"&date={days}" if days else ""
    rows = get_json(f"{OPENDOTA_URL}/players/{account_id}/heroes?lobby_type=7{q}", timeout=60)
    if not isinstance(rows, list):
        return {}
    return {int(r["hero_id"]): (int(r["games"]), int(r.get("win") or 0))
            for r in rows if r.get("hero_id") and r.get("games")}


def store_player(conn: sqlite3.Connection, account_id: int, career: dict[int, tuple[int, int]],
                 recent: dict[int, tuple[int, int]], fetched_at: int | None = None) -> None:
    rows = [(account_id, h, g, w, *recent.get(h, (0, 0))) for h, (g, w) in career.items()]
    with transaction(conn):
        conn.execute("DELETE FROM ext_player_hero WHERE account_id = ?", (account_id,))
        conn.executemany("INSERT INTO ext_player_hero(account_id, hero_id, games, wins, games_recent,"
                         " wins_recent) VALUES (?,?,?,?,?,?)", rows)
        conn.execute(
            "INSERT INTO ext_player(account_id, fetched_at, games, wins, games_recent, wins_recent)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET fetched_at=excluded.fetched_at,"
            " games=excluded.games, wins=excluded.wins, games_recent=excluded.games_recent,"
            " wins_recent=excluded.wins_recent",
            (account_id, fetched_at or int(time.time()), sum(g for g, _ in career.values()),
             sum(w for _, w in career.values()), sum(g for g, _ in recent.values()),
             sum(w for _, w in recent.values())))


def refresh_players(conn: sqlite3.Connection, accounts: Iterable[int],
                    limit: int = PLAYERS_PER_RUN) -> int:
    """Освежить историю игроков, начиная с тех, кого нет или кто давно не обновлялся."""
    fresh = {r[0]: r[1] for r in conn.execute("SELECT account_id, fetched_at FROM ext_player")}
    now = int(time.time())
    queue = [a for a in dict.fromkeys(accounts) if a > 0 and now - fresh.get(a, 0) > PLAYER_TTL]
    done = 0
    for acc in queue[:limit]:
        try:
            career = _player_heroes(acc, None)
            time.sleep(1.1)
            recent = _player_heroes(acc, RECENT_DAYS) if career else {}
        except FetchError as exc:
            if "HTTP 429" in str(exc):
                break               # лимит OpenDota — продолжим в следующий проход
            log.warning("opendota %s: %s", acc, exc)
            continue
        store_player(conn, acc, career, recent)
        done += 1
        time.sleep(1.1)
    return done


def player_queue(conn: sqlite3.Connection) -> list[int]:
    """Сначала текущие составы, потом все, кто играл в лиге (свежие первыми)."""
    live = [r[0] for r in conn.execute("SELECT account_id FROM live_team_players WHERE account_id > 0")]
    current = [r[0] for r in conn.execute(
        "SELECT account_id FROM players WHERE roster_confirmed = 1 AND account_id > 0")]
    history = [r[0] for r in conn.execute(
        "SELECT account_id FROM match_players GROUP BY account_id ORDER BY MAX(match_id) DESC")]
    return live + current + history


# --- оркестровка --------------------------------------------------------------

def refresh_external(conn: sqlite3.Connection, settings: Settings) -> dict:
    report: dict = {}
    token = stratz_token(conn, settings)
    if token:
        try:
            if int(get_meta(conn, "meta_fetched_at", "0") or 0) < time.time() - 86400:
                report["meta_rows"] = refresh_meta(conn, token)
                set_meta(conn, "meta_fetched_at", str(int(time.time())))
            # матчапы — с окна перед первым матчем лиги, но не раньше первой недели меты:
            # дальше в прошлое у STRATZ данных нет
            first = conn.execute("SELECT MIN(start_time) FROM matches").fetchone()[0]
            oldest = (first or int(time.time())) - (LOOKBACK_WEEKS + 2) * WEEK
            meta_first = conn.execute("SELECT MIN(week) FROM ext_hero_week").fetchone()[0]
            report["matchup_weeks"] = refresh_matchups(conn, token, max(oldest, meta_first or 0))
        except ExternalError as exc:
            report["stratz_error"] = str(exc)[:300]
    else:
        report["stratz_error"] = "не задан ключ STRATZ"
    try:
        report["players"] = refresh_players(conn, player_queue(conn))
    except FetchError as exc:
        report["opendota_error"] = str(exc)[:300]
    if report.get("meta_rows") or report.get("matchup_weeks") or report.get("players"):
        set_meta(conn, "external_version", str(time.time_ns()))
    return report


def coverage(conn: sqlite3.Connection) -> dict:
    meta = conn.execute("SELECT COUNT(DISTINCT week), MAX(week) FROM ext_hero_week").fetchone()
    mu = conn.execute("SELECT COUNT(DISTINCT week), MAX(week) FROM ext_matchup_week").fetchone()
    pl = conn.execute("SELECT COUNT(*), SUM(games > 0) FROM ext_player").fetchone()
    need = conn.execute("SELECT COUNT(DISTINCT account_id) FROM match_players").fetchone()[0]
    return {"meta_weeks": meta[0], "meta_latest": meta[1], "matchup_weeks": mu[0],
            "matchup_latest": mu[1], "players": pl[0], "players_public": pl[1] or 0,
            "players_needed": need}


# --- признаки драфта из внешних данных ---------------------------------------

EXTERNAL_FEATURES = ("meta", "vs", "with", "pub_exp", "pub_wr")


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


@dataclass
class PlayerHeroStats:
    games: float           # с учётом веса старых игр
    wins: float
    games_career: int
    wins_career: int
    games_recent: int
    wins_recent: int


@dataclass
class ExternalData:
    """Мета, матчапы и рейтинговая история игроков: снимок из базы для расчёта признаков."""

    meta_weeks: list[int]
    meta: dict[int, dict[int, tuple[int, int]]]
    mu_weeks: list[int]
    matchups: dict[int, dict[str, tuple[array, array]]]
    players: dict[int, tuple[dict[int, PlayerHeroStats], float]]
    exp_center: float
    _windows: OrderedDict = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)   # прогнозы идут из разных потоков

    @classmethod
    def empty(cls) -> "ExternalData":
        return cls([], {}, [], {}, {}, 0.0)

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> "ExternalData":
        meta: dict[int, dict[int, tuple[int, int]]] = {}
        for h, w, n, k in conn.execute("SELECT hero_id, week, matches, wins FROM ext_hero_week"):
            meta.setdefault(w, {})[h] = (n, k)
        matchups: dict[int, dict[str, tuple[array, array]]] = {}
        for w, kind, n, s in conn.execute("SELECT week, kind, n, s FROM ext_matchup_week"):
            an, as_ = array("f"), array("f")
            an.frombytes(n)
            as_.frombytes(s)
            matchups.setdefault(w, {})[kind] = (an, as_)
        heroes: dict[int, dict[int, PlayerHeroStats]] = {}
        for acc, h, g, k, gr, kr in conn.execute(
                "SELECT account_id, hero_id, games, wins, games_recent, wins_recent FROM ext_player_hero"):
            heroes.setdefault(acc, {})[h] = PlayerHeroStats(
                games=gr + OLD_GAMES_WEIGHT * (g - gr), wins=kr + OLD_GAMES_WEIGHT * (k - kr),
                games_career=g, wins_career=k, games_recent=gr, wins_recent=kr)
        players = {}
        logs = []
        for acc, d in heroes.items():
            games = sum(s.games for s in d.values())
            if games >= 30:
                players[acc] = (d, sum(s.wins for s in d.values()) / games)
                logs.extend(math.log1p(s.games) for s in d.values())
        return cls(meta_weeks=sorted(meta), meta=meta, mu_weeks=sorted(matchups),
                   matchups=matchups, players=players,
                   exp_center=sum(logs) / len(logs) if logs else 0.0)

    def available(self) -> dict[str, bool]:
        return {"meta": bool(self.meta_weeks), "vs": bool(self.mu_weeks),
                "with": bool(self.mu_weeks), "pub_exp": bool(self.players),
                "pub_wr": bool(self.players)}

    def _window(self, weeks: list[int], t: int) -> tuple[int, ...]:
        """Полные недели перед моментом t (последние LOOKBACK_WEEKS)."""
        i = bisect.bisect_right(weeks, t - WEEK)
        return tuple(weeks[max(0, i - LOOKBACK_WEEKS):i])

    def _cached(self, key: tuple, build):
        with self._lock:
            if key in self._windows:
                self._windows.move_to_end(key)
                return self._windows[key]
            value = build()
            self._windows[key] = value
            if len(self._windows) > 16:
                self._windows.popitem(last=False)
            return value

    def _meta_window(self, t: int) -> dict[int, tuple[int, int]]:
        weeks = self._window(self.meta_weeks, t)

        def build() -> dict[int, tuple[int, int]]:
            agg: dict[int, tuple[int, int]] = {}
            for w in weeks:
                for h, (n, k) in self.meta.get(w, {}).items():
                    a = agg.get(h, (0, 0))
                    agg[h] = (a[0] + n, a[1] + k)
            return agg
        return self._cached(("meta", weeks), build)

    def _matchup_window(self, kind: str, t: int) -> tuple[array, array]:
        weeks = self._window(self.mu_weeks, t)

        def build() -> tuple[array, array]:
            n, s = array("d", bytes(8 * SIZE * SIZE)), array("d", bytes(8 * SIZE * SIZE))
            for w in weeks:
                pair = self.matchups.get(w, {}).get(kind)
                if pair is None:
                    continue
                wn, ws = pair
                for i, v in enumerate(wn):
                    if v:
                        n[i] += v
                        s[i] += ws[i]
            return n, s
        return self._cached((kind, weeks), build)

    def hero_meta(self, hero: int, t: int) -> float:
        n, w = self._meta_window(t).get(hero, (0, 0))
        return _logit((w + 50) / (n + 100))

    def advantage(self, a: int, b: int, t: int) -> float:
        """Перевес героя a над b (доля победы сверх ожидаемой), усреднённый с обеих сторон."""
        if a >= SIZE or b >= SIZE:
            return 0.0
        n, s = self._matchup_window("vs", t)
        i, j = a * SIZE + b, b * SIZE + a
        return (s[i] / (n[i] + 50) - s[j] / (n[j] + 50)) / 200.0

    def synergy(self, a: int, b: int, t: int) -> float:
        if a >= SIZE or b >= SIZE:
            return 0.0
        n, s = self._matchup_window("with", t)
        i, j = a * SIZE + b, b * SIZE + a
        return (s[i] / (n[i] + 50) + s[j] / (n[j] + 50)) / 200.0

    def player_hero(self, account_id: int | None, hero: int) -> tuple[PlayerHeroStats, float] | None:
        """Статистика игрока на герое и его общий винрейт; None — истории нет."""
        info = self.players.get(account_id) if account_id is not None else None
        if info is None:
            return None
        stats = info[0].get(hero) or PlayerHeroStats(0.0, 0.0, 0, 0, 0, 0)
        return stats, info[1]

    def side(self, picks: list[tuple[int | None, int | None]], enemy: list[int], t: int) -> dict:
        out = dict.fromkeys(EXTERNAL_FEATURES, 0.0)
        av = self.available()
        heroes = [h for _, h in picks if h is not None]
        for acc, hero in picks:
            if hero is None:
                continue
            if av["meta"]:
                out["meta"] += self.hero_meta(hero, t)
            if av["vs"]:
                out["vs"] += sum(self.advantage(hero, e, t) for e in enemy)
            ph = self.player_hero(acc, hero)
            if ph is not None:
                st, base = ph
                out["pub_exp"] += math.log1p(st.games) - self.exp_center
                out["pub_wr"] += _logit((st.wins + PUB_SHRINK * base) / (st.games + PUB_SHRINK)) - _logit(base)
        if av["with"]:
            for i in range(len(heroes)):
                for j in range(i + 1, len(heroes)):
                    out["with"] += self.synergy(heroes[i], heroes[j], t)
        return out

    def delta(self, a: list[tuple[int | None, int | None]], b: list[tuple[int | None, int | None]],
              t: int) -> dict[str, float]:
        ha = [h for _, h in a if h is not None]
        hb = [h for _, h in b if h is not None]
        fa, fb = self.side(a, hb, t), self.side(b, ha, t)
        # перевес в матчапах уже антисимметричен: считаем его один раз, со стороны A
        return {k: (fa[k] if k == "vs" else fa[k] - fb[k]) for k in EXTERNAL_FEATURES}
