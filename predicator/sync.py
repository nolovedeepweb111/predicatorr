"""Фоновое обновление данных.

Быстрый проход (раз в пару минут): бэкап pari-mixer, если изменился, mixer-cup (составы,
замены, результаты), закрытие ставок. Полный проход (раз в 15 минут и по кнопке) — ещё
мета, матчапы и рейтинговая история игроков: они медленные и с лимитами.
"""

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


def sync_once(settings: Settings, force_backup: bool = False, full: bool = True) -> dict:
    conn = connect(settings.db_path)
    report: dict = {"started_at": int(time.time()), "full": full}
    try:
        try:
            report["backup"] = refresh_backup(conn, settings.backup_url, force=force_backup,
                                              token=settings.backup_token,
                                              fallback=settings.backup_fallback_url)
        except (FetchError, ValueError) as exc:
            report["backup"] = {"error": str(exc)[:300]}
        if settings.mixer_live:
            report["mixer"] = sync_all(conn)
        report["settled_bets"] = auto_settle(conn)
        if settings.external_enabled and full:
            # последним: история игроков с OpenDota качается минуту-две
            try:
                report["external"] = refresh_external(conn, settings)
            except Exception as exc:  # noqa: BLE001 — внешние источники не ломают остальное
                log.exception("external refresh failed")
                report["external"] = {"error": str(exc)[:300]}
        elif settings.external_enabled:
            # быстрый проход внешние данные не трогает — показываем итог последнего полного
            prev = last_sync(conn) or {}
            if "external" in prev:
                report["external"] = prev["external"]
        report["finished_at"] = int(time.time())
        set_meta(conn, "last_sync", json.dumps(report, ensure_ascii=False))
        return report
    finally:
        conn.close()


def last_sync(conn) -> dict | None:
    raw = get_meta(conn, "last_sync")
    return json.loads(raw) if raw else None


class SyncThread(threading.Thread):
    """Быстрые проходы раз в fast_sync_seconds, полные — раз в sync_minutes.
    `poke()` запускает внеочередной полный проход."""

    def __init__(self, settings: Settings, on_change=None) -> None:
        super().__init__(daemon=True, name="predicator-sync")
        self.settings = settings
        self.on_change = on_change
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._poked = False
        self.running = False

    def poke(self) -> None:
        self._poked = True
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        last_full = 0.0
        while not self._stop.is_set():
            full = self._poked or self.settings.sync_minutes <= 0 or \
                time.time() - last_full >= self.settings.sync_minutes * 60
            self._poked = False
            self.running = True
            try:
                report = sync_once(self.settings, full=full)
                if full:
                    last_full = time.time()
                # быстрые проходы идут каждые пару минут — в журнал только с новостями
                news = full or (report.get("backup") or {}).get("changed") or report.get("settled_bets")
                log.log(logging.INFO if news else logging.DEBUG, "sync: %s", report)
                if self.on_change:
                    self.on_change(report)
            except Exception:  # noqa: BLE001 — фоновый поток не должен умирать
                log.exception("sync failed")
            finally:
                self.running = False
            self._wake.wait(timeout=self.wait_seconds())
            self._wake.clear()

    def wait_seconds(self) -> float | None:
        """Сколько ждать до следующего прохода; None — до кнопки."""
        if self.settings.sync_minutes <= 0:
            return None
        full = self.settings.sync_minutes * 60
        fast = self.settings.fast_sync_seconds
        return min(fast, full) if fast > 0 else full
