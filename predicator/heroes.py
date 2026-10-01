"""Справочник героев Dota 2 (из dotaconstants) с русскими названиями для поиска."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
CDN = "https://cdn.cloudflare.steamstatic.com"


@lru_cache(maxsize=1)
def heroes() -> tuple[dict, ...]:
    base = json.loads((DATA / "heroes.json").read_text(encoding="utf-8"))
    ru = json.loads((DATA / "heroes_ru.json").read_text(encoding="utf-8"))
    out = []
    for h in base:
        names = ru.get(str(h["id"]), [])
        out.append({
            **h,
            "name_ru": names[0] if names else h["name"],
            "aliases": names[1:],
            "img_url": CDN + h["img"],
        })
    return tuple(out)


@lru_cache(maxsize=1)
def hero_by_id() -> dict[int, dict]:
    return {h["id"]: h for h in heroes()}


def hero_name(hero_id: int | None) -> str:
    h = hero_by_id().get(hero_id) if hero_id is not None else None
    return h["name"] if h else f"#{hero_id}"
