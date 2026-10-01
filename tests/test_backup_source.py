from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from predicator import sync as sync_mod
from predicator.dataset import load_dataset
from predicator.features import History
from predicator.importer import backup_status, refresh_backup
from predicator.sync import SyncThread, sync_once

TOKEN = "export-secret"


class Source:
    """Локальная «выгрузка pari-mixer»: токен, ETag, журнал заголовков."""

    def __init__(self, body: bytes, token: str | None = TOKEN):
        self.body, self.token, self.seen = body, token, []
        src = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                src.seen.append(dict(self.headers))
                if src.token and self.headers.get("X-Export-Token") != src.token:
                    self.send_response(403)
                    self.end_headers()
                    return
                etag = '"' + hashlib.sha256(src.body).hexdigest()[:16] + '"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("ETag", etag)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(src.body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/export/backup"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def payload(backup) -> dict:
    return {k: v for k, v in backup.items() if not k.startswith("_")}


def test_local_export_with_token_and_etag(conn, payload):
    src = Source(json.dumps(payload).encode())
    try:
        first = refresh_backup(conn, src.url, force=True, token=TOKEN)
        assert first["changed"] is True and first["source"] == src.url
        again = refresh_backup(conn, src.url, token=TOKEN)
        assert again == {"changed": False, "source": src.url}
        assert src.seen[-1].get("If-None-Match")          # второй раз спросили с ETag
        assert all(h.get("X-Export-Token") == TOKEN for h in src.seen)
        st = backup_status(conn)
        assert st["source"] == "local" and st["checked_at"] and st["changed_at"]
    finally:
        src.close()


def test_fallback_never_receives_token(conn, payload):
    primary = Source(json.dumps(payload).encode(), token="other")    # наш токен не подходит
    fallback = Source(json.dumps(payload).encode(), token=None)
    try:
        res = refresh_backup(conn, primary.url, force=True, token=TOKEN, fallback=fallback.url)
        assert res["changed"] is True and res["source"] == fallback.url
        assert "HTTP 403" in res["primary_error"]
        assert all("X-Export-Token" not in h for h in fallback.seen)
    finally:
        primary.close()
        fallback.close()


def test_both_sources_down_raises(conn):
    from predicator.http import FetchError
    with pytest.raises(FetchError):
        refresh_backup(conn, "http://127.0.0.1:9/backup.json", fallback="http://127.0.0.1:9/b.json")


def test_matches_without_stats_do_not_touch_gold(conn, payload):
    """Свежий матч из Steam без золота от OpenDota: идёт в форму и Эло, но не в долю золота."""
    ds = load_dataset(conn)
    before = History.build(ds.matches)
    mid, rows = next(iter(payload["match_players"].items()))
    blank = {**payload, "match_players": {**payload["match_players"],
                                          mid: [r[:4] + [None] * 6 for r in rows]}}
    from predicator.importer import import_backup
    import_backup(conn, blank)
    ds2 = load_dataset(conn)
    m = next(x for x in ds2.matches if x.match_id == int(mid))
    assert m.has_stats is False and m.complete
    after = History.build(ds2.matches)
    acc = rows[0][0]
    st0, st1 = before.players[acc], after.players[acc]
    assert st1.games == st0.games                       # матч по-прежнему учтён
    assert st1.stat_games == st0.stat_games - 1         # а в золото — нет
    gold = after.gold(acc)
    assert gold is None or 0.05 < gold < 5              # не обвалилось к нулю


def test_fast_pass_skips_external(conn, settings, monkeypatch, tmp_path, payload):
    path = tmp_path / "backup.json"
    path.write_text(json.dumps(payload))
    calls = []
    monkeypatch.setattr(sync_mod, "refresh_external", lambda c, s: calls.append(1) or {"players": 3})
    cfg = replace(settings, backup_url=str(path), backup_fallback_url="", external_enabled=True)
    full = sync_once(cfg, full=True)
    fast = sync_once(cfg, full=False)
    assert calls == [1] and full["external"] == {"players": 3}
    assert fast["full"] is False and fast["external"] == {"players": 3}   # итог полного прохода


def test_sync_intervals(settings):
    t = SyncThread(replace(settings, sync_minutes=15, fast_sync_seconds=120))
    assert t.wait_seconds() == 120
    assert SyncThread(replace(settings, sync_minutes=15, fast_sync_seconds=0)).wait_seconds() == 900
    assert SyncThread(replace(settings, sync_minutes=0)).wait_seconds() is None
