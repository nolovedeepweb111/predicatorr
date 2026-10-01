"""Фоновое обновление данных: бэкап донора, mixer-cup, мета и матчапы, закрытие ставок."""

from __future__ import annotations

import json
import logging
import threading
import time

from .bets import auto_settle
from .config import Settings
from .db import connect, get_meta, set_meta
from .external import refresh_external
from .http import FetchError
from .importer import refresh_backup
from .mixercup import sync_all

log = logging.getLogger("predicator.sync")


def sync_once(settings: Settings, force_backup: bool = False) -> dict:
    conn = connect(settings.db_path)
    report: dict = {"started_at": int(time.time())}
    try:
        try:
            report["backup"] = refresh_backup(conn, settings.backup_url, force=force_backup)
        except (FetchError, ValueError) as exc:
            report["backup"] = {"error": str(exc)[:300]}
        if settings.mixer_live:
            report["mixer"] = sync_all(conn)
        report["settled_bets"] = auto_settle(conn)
        if settings.external_enabled:
            # последним: история игроков с OpenDota качается минуту-две
            try:
                report["external"] = refresh_external(conn, settings)
            except Exception as exc:  # noqa: BLE001 — внешние источники не ломают остальное
                log.exception("external refresh failed")
                report["external"] = {"error": str(exc)[:300]}
        report["finished_at"] = int(time.time())
        set_meta(conn, "last_sync", json.dumps(report, ensure_ascii=False))
        return report
    finally:
        conn.close()


def last_sync(conn) -> dict | None:
    raw = get_meta(conn, "last_sync")
    return json.loads(raw) if raw else None


class SyncThread(threading.Thread):
    """Раз в N минут обновляет данные; `poke()` запускает внеочередной проход."""

    def __init__(self, settings: Settings, on_change=None) -> None:
        super().__init__(daemon=True, name="predicator-sync")
        self.settings = settings
        self.on_change = on_change
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.running = False

    def poke(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            self.running = True
            try:
                report = sync_once(self.settings)
                log.info("sync: %s", report)
                if self.on_change:
                    self.on_change(report)
            except Exception:  # noqa: BLE001 — фоновый поток не должен умирать
                log.exception("sync failed")
            finally:
                self.running = False
            minutes = self.settings.sync_minutes
            if minutes <= 0:
                self._wake.wait()
            else:
                self._wake.wait(timeout=minutes * 60)
            self._wake.clear()
