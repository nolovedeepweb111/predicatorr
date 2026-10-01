"""Настройки из переменных окружения (и необязательного файла .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Минимальный разбор .env: KEY=VALUE, комментарии через #. Окружение важнее файла."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# Публичный бэкап pari-mixer на GitHub. GitHub обновляет его раз в несколько часов, поэтому
# на одном сервере с pari-mixer лучше читать его локальную выгрузку (docs/pari-mixer-export.md),
# а этот адрес оставить запасным.
GITHUB_BACKUP_URL = ("https://raw.githubusercontent.com/nolovedeepweb111/pari-mixer-scraper/"
                     "data-backup/backup.json")


@dataclass(frozen=True)
class MixerApi:
    """Одна копия платформы mixer-cup. Номера турниров сдвигаются, чтобы стать глобальными."""

    code: str
    title: str
    url: str
    league_id: int
    offset: int
    has_weeks: bool


MIXER_APIS: tuple[MixerApi, ...] = (
    MixerApi("pari", "PARI Mixer Cup", "https://api.mixer-cup.gg", 19924, 0, False),
    MixerApi("winline", "WINLINE Super Mixer",
             "https://api.mixer-cup.sportpostproduction.com", 20165, 20000, True),
    MixerApi("pari_super", "PARI Super Mixer",
             "https://api.pari-super-mixer.sportpostproduction.com", 19965, 30000, True),
)


def series_of(tournament_id: int | None) -> MixerApi | None:
    """По глобальному номеру турнира вернуть серию (PARI, WINLINE, PARI Super)."""
    if tournament_id is None:
        return None
    for api in sorted(MIXER_APIS, key=lambda a: -a.offset):
        if tournament_id > api.offset:
            return api
    return None


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(
        _env("PREDICATOR_DB", str(ROOT / "var" / "predicator.sqlite3"))))
    backup_url: str = field(default_factory=lambda: _env("PREDICATOR_BACKUP_URL", GITHUB_BACKUP_URL))
    # Токен локальной выгрузки pari-mixer (заголовок X-Export-Token); уходит только на backup_url.
    backup_token: str = field(default_factory=lambda: _env("PREDICATOR_BACKUP_TOKEN", ""))
    # Откуда брать бэкап, если backup_url не отвечает.
    backup_fallback_url: str = field(default_factory=lambda: _env(
        "PREDICATOR_BACKUP_FALLBACK_URL", GITHUB_BACKUP_URL))
    # Полный проход (с метой, матчапами и историей игроков) — раз в N минут; 0 — только по кнопке.
    sync_minutes: int = field(default_factory=lambda: int(_env("PREDICATOR_SYNC_MINUTES", "15")))
    # Между полными — быстрые проходы: бэкап (с ETag), mixer-cup, закрытие ставок; 0 — без них.
    fast_sync_seconds: int = field(default_factory=lambda: int(_env("PREDICATOR_FAST_SYNC_SECONDS", "120")))
    mixer_live: bool = field(default_factory=lambda: _env("PREDICATOR_MIXER_LIVE", "1") == "1")
    pari_hosts: tuple[str, ...] = field(default_factory=lambda: tuple(
        h.strip().rstrip("/") for h in _env(
            "PARI_HOSTS",
            "https://line-lb51-w.pb06e2-resources.com,"
            "https://line-lb01-w.pb06e2-resources.com,"
            "https://line-vk01-w.pb06e2-resources.ru").split(",") if h.strip()))
    pari_scope_market: str = field(default_factory=lambda: _env("PARI_SCOPE_MARKET", "2300"))
    pari_enabled: bool = field(default_factory=lambda: _env("PARI_ENABLED", "1") == "1")
    # Кэш линии PARI в секундах: чаще не дёргаем, даже если страницу обновляют.
    pari_cache_seconds: int = field(default_factory=lambda: int(_env("PARI_CACHE_SECONDS", "60")))
    password: str = field(default_factory=lambda: _env("PREDICATOR_PASSWORD", ""))
    # Ключ STRATZ для меты и матчапов; можно задать и на сайте (Модель → Источники данных).
    stratz_token: str = field(default_factory=lambda: _env("STRATZ_TOKEN", ""))
    external_enabled: bool = field(default_factory=lambda: _env("PREDICATOR_EXTERNAL", "1") == "1")
    host: str = field(default_factory=lambda: _env("PREDICATOR_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("PREDICATOR_PORT", "8000")))


def get_settings() -> Settings:
    return Settings()
