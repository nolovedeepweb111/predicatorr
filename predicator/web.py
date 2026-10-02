"""Веб-приложение: API + статический фронтенд."""

from __future__ import annotations

import base64
import binascii
import calendar
import hmac
import logging
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import bets as bets_mod
from .backtest import run_backtest
from .config import Settings, get_settings
from .db import connect, set_setting
from .external import coverage as external_coverage
from .external import stratz_token
from .heroes import heroes
from .http import FetchError
from .pari import PariLine, best_team, link_events
from .rosters import (add_change, default_tournament, delete_change, ensure_local_player,
                      search_players, tournaments)
from .service import PredictorService
from .importer import backup_status
from . import paper
from .live import LiveFeed
from .sync import SyncThread, last_sync, sync_once

STATIC = Path(__file__).resolve().parent / "static"
log = logging.getLogger("predicator.web")


class PredictIn(BaseModel):
    tournament_id: int
    team_a: str
    team_b: str
    lineup_a: list[int] | None = None
    lineup_b: list[int] | None = None
    heroes_a: list[int | None] | None = None
    heroes_b: list[int | None] | None = None
    k_a: float | None = Field(None, gt=1.0)
    k_b: float | None = Field(None, gt=1.0)
    in_play: bool = False             # коэффициенты из лайва: игра уже идёт


class AnalyseIn(BaseModel):
    p_a: float = Field(..., gt=0.0, lt=1.0)
    k_a: float = Field(..., gt=1.0)
    k_b: float = Field(..., gt=1.0)


class ChangeIn(BaseModel):
    tournament_id: int
    team_key: str
    out_account: int
    in_account: int | None = None
    in_name: str | None = None        # игрок из очереди без Steam-аккаунта
    in_mmr: float | None = None
    note: str | None = None


class BetIn(BaseModel):
    tournament_id: int | None = None
    team_a_key: str | None = None
    team_b_key: str | None = None
    team_a: str
    team_b: str
    pick: str = Field(..., pattern="^[AB]$")
    odds: float = Field(..., gt=1.0)
    stake: float = Field(..., gt=0.0)
    model_prob: float | None = None
    book_prob: float | None = None
    stage: str | None = None
    pari_event_id: int | None = None
    note: str | None = None
    snapshot: dict | None = None


class BetPatch(BaseModel):
    status: str = Field(..., pattern="^(open|won|lost|void)$")


class TokenIn(BaseModel):
    token: str = Field("", max_length=4000)


class SettingsIn(BaseModel):
    bankroll: float | None = Field(None, ge=0)
    kelly_fraction: float | None = Field(None, ge=0, le=1)
    max_stake_pct: float | None = Field(None, ge=0, le=1)
    min_edge: float | None = Field(None, ge=-1, le=1)


def create_app(settings: Settings | None = None, start_sync: bool = True) -> FastAPI:
    settings = settings or get_settings()
    service = PredictorService(settings)
    pari = PariLine(settings.pari_hosts, settings.pari_scope_market, settings.pari_cache_seconds)
    live = LiveFeed(settings)
    sync = SyncThread(settings)
    backtest_cache: dict[str, dict] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_sync:
            sync.start()
            threading.Thread(target=paper_loop, daemon=True, name="predicator-paper").start()
        yield
        paper_stop.set()
        sync.stop()

    app = FastAPI(title="Predicatorr", lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json")
    app.state.service = service
    app.state.pari = pari
    app.state.sync = sync

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        if settings.password:
            ok = False
            header = request.headers.get("authorization", "")
            if header.lower().startswith("basic "):
                try:
                    _, _, pwd = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                    ok = hmac.compare_digest(pwd, settings.password)
                except (binascii.Error, UnicodeDecodeError):
                    ok = False
            if not ok:
                return Response(status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="predicatorr"'})
        return await call_next(request)

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def require_model(conn: sqlite3.Connection) -> None:
        service.refresh(conn)
        if not service.ready:
            raise HTTPException(503, service.status()["error"] or "данные ещё загружаются")

    def resolve_tournament(conn: sqlite3.Connection, tournament_id: int | None) -> int:
        tid = tournament_id or default_tournament(conn)
        if tid is None:
            raise HTTPException(404, "нет ни одного турнира с составами")
        return tid

    # --- статус ----------------------------------------------------------

    @app.get("/api/status")
    def status(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        try:
            service.refresh(conn)
        except Exception as exc:  # noqa: BLE001
            log.exception("refresh failed")
            return {"model": {"ready": False, "error": str(exc)}}
        return {
            "model": service.status(),
            "sync": last_sync(conn),
            "sync_running": sync.running,
            "backup": backup_status(conn),
            "live": live.status(),
            "pari": {**pari.status(), "enabled": settings.pari_enabled},
            "external": {**external_coverage(conn), "enabled": settings.external_enabled,
                         "stratz_token_set": bool(stratz_token(conn, settings))},
            "default_tournament": default_tournament(conn),
            "now": int(time.time()),
        }

    @app.post("/api/sync")
    def sync_now() -> dict:
        if sync.is_alive():
            sync.poke()
        else:   # фон выключен (--no-sync): разовый проход по кнопке
            threading.Thread(target=sync_once, args=(settings,), daemon=True).start()
        return {"ok": True}

    @app.get("/api/tournaments")
    def get_tournaments(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return {"tournaments": tournaments(conn), "default": default_tournament(conn)}

    @app.get("/api/heroes")
    def get_heroes(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        require_model(conn)
        stats = {h["hero_id"]: h for h in service.hero_table()}
        return {"heroes": [{**h, **stats.get(h["id"], {})} for h in heroes()]}

    # --- команды и прогноз -----------------------------------------------

    @app.get("/api/teams")
    def get_teams(tournament_id: int | None = None,
                  conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        require_model(conn)
        tid = resolve_tournament(conn, tournament_id)
        ctx = service.cup_context(conn, tid)
        out = []
        for t in ctx.teams.values():
            d = t.as_dict()
            for p in d["players"]:
                # для показа — вся история, включая игры идущего кубка
                p.update(service.player_stats(p["account_id"], service.model.hist))
                p["top_heroes"] = service.top_heroes(p["account_id"])
            if len(t.lineup) >= 5:
                r = service.team_rating(ctx, t.lineup[:5])
                d["rating"] = round(r["rating"], 4)
                d["score"] = r["score"]
                d["form"] = r["form"]
                d["contrib"] = r["contrib"]
            d["complete"] = len(t.lineup) >= 5
            out.append(d)
        ranked = sorted((d for d in out if d["complete"]), key=lambda d: -d["rating"])
        for i, d in enumerate(ranked):
            d["rank"] = i + 1
        return {"tournament_id": tid, "games_played": ctx.games_played, "teams": out}

    @app.post("/api/predict")
    def predict(body: PredictIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        require_model(conn)
        if body.team_a == body.team_b:
            raise HTTPException(400, "выберите две разные команды")
        try:
            res = service.predict(conn, body.tournament_id, body.team_a, body.team_b,
                                  body.lineup_a, body.lineup_b, body.heroes_a, body.heroes_b)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        # Лайв: перевес считаем только до горна и с полным драфтом — дальше линия PARI
        # учитывает ход игры, а прогноз нет. Время игры знает pari-mixer (Steam).
        game = live_game_for(conn, body.tournament_id, body.team_a, body.team_b) if body.in_play else None
        if game:
            res["live"] = {k: game[k] for k in ("game_time", "picks", "assigned", "draft_complete",
                                                 "before_horn")}
        window = bool(game and game["before_horn"] and res["draft"] and res["draft"]["heroes"] == 10)
        if body.k_a and body.k_b:
            res["offer"] = bets_mod.Offer(body.k_a, body.k_b, res["p_a"]).analyse(
                bets_mod.get_bet_settings(conn), in_play=body.in_play and not window)
            res["offer"]["draft_window"] = window
        return res

    def live_game_for(conn: sqlite3.Connection, tid: int, a: str, b: str) -> dict | None:
        try:
            games = service.live_games(conn, tid, live)
        except (FetchError, RuntimeError):
            return None
        return next((g for g in games if set(g["team_keys"].values()) == {a, b}), None)

    @app.get("/api/live")
    def live_games(tournament_id: int | None = None,
                   conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        """Идущие игры кубка: команды, время, драфт и герои по игрокам (из pari-mixer)."""
        if not live.enabled:
            return {"enabled": False, "games": []}
        require_model(conn)
        tid = resolve_tournament(conn, tournament_id)
        return {**live.status(), "tournament_id": tid, "games": service.live_games(conn, tid, live)}

    @app.post("/api/odds/analyse")
    def analyse(body: AnalyseIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return bets_mod.Offer(body.k_a, body.k_b, body.p_a).analyse(bets_mod.get_bet_settings(conn))

    # --- игроки и замены -------------------------------------------------

    @app.get("/api/players/search")
    def players_search(q: str, tournament_id: int | None = None,
                       conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return {"players": search_players(conn, q, tournament_id)}

    @app.get("/api/players/{account_id}")
    def player(account_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        require_model(conn)
        row = conn.execute("SELECT * FROM players WHERE account_id = ?", (account_id,)).fetchone()
        if not row:
            raise HTTPException(404, "игрок не найден")
        return {**dict(row), **service.player_stats(account_id, service.model.hist),
                "top_heroes": service.top_heroes(account_id, 20)}

    @app.post("/api/rosters/change")
    def roster_change(body: ChangeIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        in_account = body.in_account
        if in_account is None:
            if not body.in_name:
                raise HTTPException(400, "укажите, кто пришёл на замену")
            in_account = ensure_local_player(conn, body.in_name.strip(), body.in_mmr)
        try:
            change_id = add_change(conn, body.tournament_id, body.team_key, body.out_account,
                                   in_account, body.note)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": change_id, "in_account": in_account}

    @app.delete("/api/rosters/change/{change_id}")
    def roster_change_delete(change_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if not delete_change(conn, change_id):
            raise HTTPException(404, "замена не найдена")
        return {"ok": True}

    @app.get("/api/schedule")
    def schedule(tournament_id: int | None = None,
                 conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        tid = resolve_tournament(conn, tournament_id)
        rows = conn.execute(
            "SELECT * FROM live_games WHERE tournament_id = ? AND (status IS NULL OR"
            " status != 'COMPLETE') ORDER BY status != 'ACTIVE', seq, game_id", (tid,)).fetchall()
        return {"games": [dict(r) for r in rows]}

    # --- линия PARI ------------------------------------------------------

    def line_events(conn: sqlite3.Connection, tid: int, force: bool = False) -> tuple[list[dict], str | None]:
        """Dota-события линии PARI, сопоставленные с командами кубка; изменения коэффициентов
        записываются (по ним потом видно открытие и цену перед началом каждой игры)."""
        error = None
        try:
            pari.refresh(force=force)
        except FetchError as exc:
            error = str(exc)
        ctx = service.cup_context(conn, tid)
        names = {k: t.name for k, t in ctx.teams.items() if len(t.lineup) >= 5}
        numbers = {k: ctx.teams[k].number for k in names}
        events = link_events(pari.dota_events(), names, numbers)
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        for ev in events:
            if ev["k1"] and ev["k2"]:
                last = conn.execute("SELECT k1, k2 FROM odds_snapshots WHERE event_id = ?"
                                    " ORDER BY fetched_at DESC LIMIT 1", (ev["event_id"],)).fetchone()
                if not last or (last["k1"], last["k2"]) != (ev["k1"], ev["k2"]):
                    conn.execute("INSERT OR IGNORE INTO odds_snapshots(event_id, fetched_at, team1,"
                                 " team2, start_time, k1, k2, team1_key, team2_key)"
                                 " VALUES (?,?,?,?,?,?,?,?,?)",
                                 (ev["event_id"], now, ev["team1"], ev["team2"], ev["start_time"],
                                  ev["k1"], ev["k2"], ev.get("team1_key"), ev.get("team2_key")))
            # В лайве PARI то и дело приостанавливает приём и на это время коэффициентов не
            # отдаёт: покажем первые и последние, что видели (без расчёта по ним).
            first = conn.execute("SELECT k1, k2 FROM odds_snapshots WHERE event_id = ?"
                                 " ORDER BY fetched_at LIMIT 1", (ev["event_id"],)).fetchone()
            ev["opening"] = dict(first) if first else None
            latest = conn.execute("SELECT k1, k2, fetched_at FROM odds_snapshots WHERE event_id = ?"
                                  " ORDER BY fetched_at DESC LIMIT 1", (ev["event_id"],)).fetchone()
            ev["last"] = None if not latest else {
                "k1": latest["k1"], "k2": latest["k2"],
                "at": calendar.timegm(time.strptime(latest["fetched_at"], "%Y-%m-%dT%H:%M:%S"))}
        return events, error

    def record_line() -> None:
        """Фоновая запись линии: открытие и цена перед началом есть у каждой игры, даже если
        сайт в это время никто не открывал."""
        if not settings.pari_enabled:
            return
        conn = connect(settings.db_path)
        try:
            service.refresh(conn)
            if service.ready:
                tid = default_tournament(conn)
                if tid is not None:
                    line_events(conn, tid)
        except Exception:  # noqa: BLE001 — запись линии не должна ронять фоновое обновление
            logging.getLogger("predicator.web").exception("PARI line recording failed")
        finally:
            conn.close()

    sync.on_change = lambda report: record_line()

    # --- эксперимент с виртуальными ставками ---------------------------------

    def closing_price(conn: sqlite3.Connection, a: str, b: str, start: int) -> tuple | None:
        """Последняя цена PARI на игру a — b, записанная до её начала: (k_a, k_b)."""
        row = conn.execute(
            "SELECT k1, k2, team1_key FROM odds_snapshots WHERE ((team1_key = ? AND team2_key = ?)"
            " OR (team1_key = ? AND team2_key = ?)) AND fetched_at <= ? AND start_time BETWEEN ? AND ?"
            " ORDER BY fetched_at DESC LIMIT 1",
            (a, b, b, a, time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start)),
             start - 4 * 3600, start + 4 * 3600)).fetchone()
        if not row or not row["k1"] or not row["k2"]:
            return None
        return (row["k1"], row["k2"]) if row["team1_key"] == a else (row["k2"], row["k1"])

    def paper_tick() -> float:
        """Шаг эксперимента; возвращает паузу до следующего (чаще, пока идёт игра)."""
        conn = connect(settings.db_path)
        try:
            service.refresh(conn)
            tid = default_tournament(conn)
            if not service.ready or tid is None:
                return 60
            events = line_events(conn, tid)[0] if settings.pari_enabled else []
            lives = service.live_games(conn, tid, live) if live.enabled else []

            def predict(a: str, b: str, **kw) -> float:
                return service.predict(conn, tid, a, b, **kw)["p_a"]
            active = paper.step(conn, tid, events, lives, predict,
                                lambda a, b, start: closing_price(conn, a, b, start))
            return 20 if active else 60
        except Exception:  # noqa: BLE001 — эксперимент не должен ронять сайт
            logging.getLogger("predicator.paper").exception("paper step failed")
            return 60
        finally:
            conn.close()

    paper_stop = threading.Event()
    app.state.paper_tick = paper_tick

    def paper_loop() -> None:
        while not paper_stop.is_set():
            paper_stop.wait(paper_tick())

    @app.get("/api/odds")
    def odds(tournament_id: int | None = None, refresh: bool = False,
             conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if not settings.pari_enabled:
            return {"enabled": False, "events": []}
        require_model(conn)
        tid = resolve_tournament(conn, tournament_id)
        events, error = line_events(conn, tid, force=refresh)
        ctx = service.cup_context(conn, tid)
        bet_settings = bets_mod.get_bet_settings(conn)
        for ev in events:
            if not ev["linked"]:
                continue
            try:
                pred = service.predict(conn, tid, ev["team1_key"], ev["team2_key"])
            except ValueError:
                continue
            game = live_game_for(conn, tid, ev["team1_key"], ev["team2_key"]) if ev["place"] == "live" else None
            if game:
                ev["live"] = {k: game[k] for k in ("game_time", "picks", "assigned", "draft_complete",
                                                    "before_horn")}
            ev["lineup_a"] = ctx.teams[ev["team1_key"]].lineup[:5]
            ev["lineup_b"] = ctx.teams[ev["team2_key"]].lineup[:5]
            ev["p_a"] = pred["p_a"]
            ev["prediction"] = {"p_a": pred["p_a"], "fair_odds": pred["fair_odds"],
                                "series": pred["series"]}
            if ev["k1"] and ev["k2"] and not ev["blocked"]:
                ev["offer"] = bets_mod.Offer(ev["k1"], ev["k2"], pred["p_a"]).analyse(
                    bet_settings, in_play=ev["place"] == "live")
        return {"enabled": True, "error": error, "status": pari.status(), "tournament_id": tid,
                "events": events}

    # --- расписание и результаты ------------------------------------------

    def upcoming_games(conn: sqlite3.Connection, tid: int, limit: int) -> list[dict]:
        ctx = service.cup_context(conn, tid)
        rows = conn.execute(
            "SELECT * FROM live_games WHERE tournament_id = ? AND (status IS NULL OR status != 'COMPLETE')"
            " ORDER BY status != 'ACTIVE', COALESCE(planned_time, 9e18), seq, game_id LIMIT ?",
            (tid, limit)).fetchall()
        events: dict[frozenset, dict] = {}
        if settings.pari_enabled:
            for ev in line_events(conn, tid)[0]:
                if ev["linked"]:
                    events[frozenset((ev["team1_key"], ev["team2_key"]))] = ev
        lives = {frozenset(g["team_keys"].values()): g for g in service.live_games(conn, tid, live)} \
            if live.enabled else {}
        bet_settings = bets_mod.get_bet_settings(conn)
        out = []
        for r in rows:
            a, b = r["team1_key"], r["team2_key"]
            if a not in ctx.teams or b not in ctx.teams:
                continue
            item = {"game_id": r["game_id"], "status": r["status"], "planned_time": r["planned_time"],
                    "start_time": r["start_time"], "week": r["week_number"],
                    "team_a": {"key": a, "name": ctx.teams[a].name},
                    "team_b": {"key": b, "name": ctx.teams[b].name}, "p_a": None}
            try:
                pred = service.predict(conn, tid, a, b)
                item["p_a"], item["fair_odds"] = pred["p_a"], pred["fair_odds"]
            except ValueError:
                pass
            ev = events.get(frozenset((a, b)))
            if ev:
                k = (ev["k1"], ev["k2"]) if ev["team1_key"] == a else (ev["k2"], ev["k1"])
                item["odds"] = {"k_a": k[0], "k_b": k[1], "place": ev["place"], "blocked": ev["blocked"]}
                if k[0] and k[1] and not ev["blocked"] and item["p_a"] is not None:
                    item["offer"] = bets_mod.Offer(k[0], k[1], item["p_a"]).analyse(
                        bet_settings, in_play=ev["place"] == "live")
            g = lives.get(frozenset((a, b)))
            if g:
                item["live"] = {k: g[k] for k in ("game_time", "picks", "assigned", "draft_complete",
                                                   "before_horn", "score")}
            out.append(item)
        return out

    def played_games(conn: sqlite3.Connection, tid: int) -> dict:
        games = [dict(g) for g in service.played_games(conn, tid)]
        # цена PARI перед началом — по записанной линии, если игра в ней была
        ctx = service.cup_context(conn, tid)
        names = {k: t.name for k, t in ctx.teams.items()}
        by_pair: dict[frozenset, list[dict]] = {}
        for row in conn.execute("SELECT event_id, fetched_at, k1, k2, team1, team2, team1_key, team2_key,"
                                " start_time FROM odds_snapshots ORDER BY fetched_at"):
            s = dict(row)
            if not s["team1_key"] or not s["team2_key"]:      # записано до привязки к командам
                s["team1_key"] = best_team(s["team1"] or "", names)[0]
                s["team2_key"] = best_team(s["team2"] or "", names)[0]
                if not s["team1_key"] or not s["team2_key"] or s["team1_key"] == s["team2_key"]:
                    continue
            by_pair.setdefault(frozenset((s["team1_key"], s["team2_key"])), []).append(s)
        pre_hits = draft_hits = pari_n = pari_hits = 0
        for g in games:
            a = g["team_a"]["key"]
            snaps = [s for s in by_pair.get(frozenset((a, g["team_b"]["key"])), [])
                     if s["start_time"] and abs(s["start_time"] - g["start_time"]) < 4 * 3600]
            if snaps:
                eid = min(snaps, key=lambda s: abs(s["start_time"] - g["start_time"]))["event_id"]
                snaps = [s for s in snaps if s["event_id"] == eid]
                before = [s for s in snaps if calendar.timegm(
                    time.strptime(s["fetched_at"], "%Y-%m-%dT%H:%M:%S")) <= g["start_time"]]

                def side(s: dict) -> list[float]:
                    return [s["k1"], s["k2"]] if s["team1_key"] == a else [s["k2"], s["k1"]]
                g["pari"] = {"opening": side(snaps[0]), "close": side(before[-1]) if before else None}
                if before:
                    k_a, k_b = side(before[-1])
                    pari_n += 1
                    pari_hits += int((k_a < k_b) == (g["winner"] == "a"))
            pre_hits += int((g["pre"] >= 0.5) == (g["winner"] == "a"))
            draft_hits += int((g["draft"] >= 0.5) == (g["winner"] == "a"))
        return {"games": games, "summary": {"games": len(games), "pre_hits": pre_hits,
                                            "draft_hits": draft_hits, "pari_games": pari_n,
                                            "pari_hits": pari_hits}}

    @app.get("/api/games")
    def games(tournament_id: int | None = None, kind: str = "upcoming", limit: int = 40,
              conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        """Будущие игры кубка (upcoming) или сыгранные с нашим прогнозом и итогом (played)."""
        if kind not in ("upcoming", "played"):
            raise HTTPException(400, "kind: upcoming или played")
        require_model(conn)
        tid = resolve_tournament(conn, tournament_id)
        if kind == "played":
            return {"tournament_id": tid, **played_games(conn, tid)}
        return {"tournament_id": tid, "games": upcoming_games(conn, tid, max(1, min(limit, 300)))}

    @app.get("/api/paper")
    def paper_report(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        """Эксперимент: банки стратегий, история банка, последние виртуальные ставки."""
        return paper.report(conn)

    @app.post("/api/paper/reset")
    def paper_reset(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        paper.reset(conn)
        return {"ok": True}

    # --- ставки ------------------------------------------------------------

    @app.get("/api/bets")
    def get_bets(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return {"bets": bets_mod.list_bets(conn), "summary": bets_mod.bet_summary(conn),
                "settings": bets_mod.get_bet_settings(conn)}

    @app.post("/api/bets")
    def post_bet(body: BetIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return {"id": bets_mod.add_bet(conn, body.model_dump())}

    @app.patch("/api/bets/{bet_id}")
    def patch_bet(bet_id: int, body: BetPatch, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if not bets_mod.settle_bet(conn, bet_id, body.status):
            raise HTTPException(404, "ставка не найдена")
        return {"ok": True}

    @app.delete("/api/bets/{bet_id}")
    def remove_bet(bet_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if not bets_mod.delete_bet(conn, bet_id):
            raise HTTPException(404, "ставка не найдена")
        return {"ok": True}

    @app.put("/api/settings/stratz-token")
    def put_stratz_token(body: TokenIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        """Ключ STRATZ хранится только на сервере; наружу отдаём лишь «задан / не задан»."""
        token = body.token.strip()
        if token:
            set_setting(conn, "stratz_token", token)
        else:
            conn.execute("DELETE FROM settings WHERE key = 'stratz_token'")
        if sync.is_alive():
            sync.poke()
        return {"stratz_token_set": bool(stratz_token(conn, settings))}

    @app.get("/api/settings")
    def get_bet_settings(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return bets_mod.get_bet_settings(conn)

    @app.put("/api/settings")
    def put_bet_settings(body: SettingsIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return bets_mod.save_bet_settings(conn, body.model_dump(exclude_none=True))

    # --- модель ------------------------------------------------------------

    @app.get("/api/backtest")
    def backtest(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        require_model(conn)
        key = service.status()["data_version"] or "0"
        if key not in backtest_cache:
            backtest_cache.clear()
            backtest_cache[key] = run_backtest(service.dataset, int(time.time()),
                                               ext=service.model.ext)
        return {**backtest_cache[key], "model": service.status()}

    # --- фронтенд ----------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.exception_handler(RuntimeError)
    async def runtime_error(request: Request, exc: RuntimeError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    return app
