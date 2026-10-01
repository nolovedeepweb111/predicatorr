"""Веб-приложение: API + статический фронтенд."""

from __future__ import annotations

import base64
import binascii
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
from .pari import PariLine, link_events
from .rosters import (add_change, default_tournament, delete_change, ensure_local_player,
                      search_players, tournaments)
from .service import PredictorService
from .importer import backup_status
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
    sync = SyncThread(settings)
    backtest_cache: dict[str, dict] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_sync:
            sync.start()
        yield
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
        if body.k_a and body.k_b:
            res["offer"] = bets_mod.Offer(body.k_a, body.k_b, res["p_a"]).analyse(
                bets_mod.get_bet_settings(conn), in_play=body.in_play)
        return res

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

    @app.get("/api/odds")
    def odds(tournament_id: int | None = None, refresh: bool = False,
             conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        if not settings.pari_enabled:
            return {"enabled": False, "events": []}
        error = None
        try:
            pari.refresh(force=refresh)
        except FetchError as exc:
            error = str(exc)
        require_model(conn)
        tid = resolve_tournament(conn, tournament_id)
        ctx = service.cup_context(conn, tid)
        names = {k: t.name for k, t in ctx.teams.items() if len(t.lineup) >= 5}
        numbers = {k: ctx.teams[k].number for k in names}
        events = link_events(pari.dota_events(), names, numbers)
        bet_settings = bets_mod.get_bet_settings(conn)
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        for ev in events:
            if ev["k1"] and ev["k2"]:
                last = conn.execute("SELECT k1, k2 FROM odds_snapshots WHERE event_id = ?"
                                    " ORDER BY fetched_at DESC LIMIT 1", (ev["event_id"],)).fetchone()
                if not last or (last["k1"], last["k2"]) != (ev["k1"], ev["k2"]):
                    conn.execute("INSERT OR IGNORE INTO odds_snapshots(event_id, fetched_at, team1,"
                                 " team2, start_time, k1, k2) VALUES (?,?,?,?,?,?,?)",
                                 (ev["event_id"], now, ev["team1"], ev["team2"], ev["start_time"],
                                  ev["k1"], ev["k2"]))
                first = conn.execute("SELECT k1, k2 FROM odds_snapshots WHERE event_id = ?"
                                     " ORDER BY fetched_at LIMIT 1", (ev["event_id"],)).fetchone()
                ev["opening"] = dict(first) if first else None
            if not ev["linked"]:
                continue
            try:
                pred = service.predict(conn, tid, ev["team1_key"], ev["team2_key"])
            except ValueError:
                continue
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
