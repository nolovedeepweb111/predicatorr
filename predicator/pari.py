"""Линия букмекера PARI.

Открытый JSON той же платформы, что и у сайта pari.ru:
    GET {host}/events/listBase?lang=ru&scopeMarket=2300        полный снимок
    GET {host}/events/list?lang=ru&version=V&scopeMarket=2300  изменения с версии V
В ответе: sports (виды спорта и турниры-«сегменты»), events (матчи и вложенные
события вроде отдельных карт), customFactors (коэффициенты события: f — код
исхода, v — коэффициент). 921 = П1 (победа первой команды), 923 = П2.

Держим в памяти только Dota-события. Полный снимок тяжёлый (мегабайты), поэтому
перекачиваем его раз в три часа, а между ними забираем только изменения.
"""

from __future__ import annotations

import difflib
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable

from .http import FetchError, get_json

WIN1, WIN2 = 921, 923
DOTA_RE = re.compile(r"dota|дота", re.I)
MIXER_RE = re.compile(r"mixer|миксер", re.I)
REBOOTSTRAP_SECONDS = 3 * 3600
STALE_SECONDS = 8 * 3600


@dataclass
class PariMarket:
    event_id: int
    name: str
    k1: float | None
    k2: float | None
    blocked: bool


@dataclass
class PariEvent:
    event_id: int
    segment: str
    team1: str
    team2: str
    start_time: int | None
    place: str
    k1: float | None
    k2: float | None
    blocked: bool
    mixer: bool
    maps: list[PariMarket] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class PariLine:
    def __init__(self, hosts: Iterable[str], scope_market: str = "2300",
                 cache_seconds: int = 60) -> None:
        self.hosts = list(hosts)
        self.scope = scope_market
        self.cache_seconds = cache_seconds
        self.sports: dict[int, dict] = {}
        self.events: dict[int, dict] = {}
        self.factors: dict[int, dict[int, dict]] = {}
        self.blocks: dict[int, str] = {}
        self.version: int | None = None
        self.bootstrapped_at = 0.0
        self.fetched_at = 0.0
        self.last_error: str | None = None
        self.host: str | None = None
        self._lock = threading.Lock()

    # --- загрузка ----------------------------------------------------------

    def _get(self, path: str) -> dict:
        errors = []
        order = ([self.host] if self.host else []) + [h for h in self.hosts if h != self.host]
        for host in order:
            try:
                data = get_json(host + path, timeout=40, headers={
                    "Origin": "https://pari.ru", "Referer": "https://pari.ru/"})
                self.host = host
                return data
            except FetchError as exc:
                errors.append(str(exc)[:200])
        raise FetchError("; ".join(errors) or "не задан ни один адрес линии PARI")

    def refresh(self, force: bool = False) -> None:
        with self._lock:
            now = time.time()
            if not force and now - self.fetched_at < self.cache_seconds:
                return
            self.fetched_at = now
            try:
                if self.version is None or now - self.bootstrapped_at > REBOOTSTRAP_SECONDS:
                    self._bootstrap()
                else:
                    self._apply(self._get(
                        f"/events/list?lang=ru&version={self.version}&scopeMarket={self.scope}"))
                self.last_error = None
            except FetchError as exc:
                self.last_error = str(exc)
                raise

    def _bootstrap(self) -> None:
        try:
            data = self._get(f"/events/listBase?lang=ru&scopeMarket={self.scope}")
        except FetchError:
            data = self._get(f"/events/list?lang=ru&version=0&scopeMarket={self.scope}")
        self.sports.clear()
        self.events.clear()
        self.factors.clear()
        self.blocks.clear()
        self._apply(data)
        self.bootstrapped_at = time.time()

    def _apply(self, packet: dict) -> None:
        for s in packet.get("sports") or []:
            if "id" in s:
                self.sports[s["id"]] = s
        events = [e for e in packet.get("events") or [] if "id" in e]
        # сначала матчи из Dota-турниров, потом вложенные в них события (карты)
        for e in events:
            if e["id"] in self.events or self._is_dota(e):
                self.events[e["id"]] = {**self.events.get(e["id"], {}), **e}
        for e in events:
            if e["id"] not in self.events and e.get("parentId") in self.events:
                self.events[e["id"]] = e
        for cf in packet.get("customFactors") or []:
            eid = cf.get("e")
            if eid not in self.events:
                continue
            cur = self.factors.setdefault(eid, {})
            for f in cf.get("factors") or []:
                cur[f.get("f")] = f
        for b in packet.get("eventBlocks") or []:
            eid = b.get("eventId")
            if eid in self.events:
                self.blocks[eid] = b.get("state") or ""
        for eid in [eid for eid, e in self.events.items()
                    if e.get("startTime") and time.time() - e["startTime"] > STALE_SECONDS]:
            self.events.pop(eid, None)
            self.factors.pop(eid, None)
            self.blocks.pop(eid, None)
        if packet.get("packetVersion") is not None:
            self.version = packet["packetVersion"]

    def _segment_name(self, sport_id: int | None) -> str:
        names = []
        seen = set()
        while sport_id is not None and sport_id in self.sports and sport_id not in seen:
            seen.add(sport_id)
            s = self.sports[sport_id]
            names.append(s.get("name") or "")
            sport_id = s.get("parentId")
        return " / ".join(reversed([n for n in names if n]))

    def _is_dota(self, event: dict) -> bool:
        return bool(DOTA_RE.search(self._segment_name(event.get("sportId"))))

    # --- выдача ------------------------------------------------------------

    def _odds(self, eid: int) -> tuple[float | None, float | None, bool]:
        fs = self.factors.get(eid, {})
        k1 = fs.get(WIN1, {}).get("v")
        k2 = fs.get(WIN2, {}).get("v")
        blocked = self.blocks.get(eid) == "blocked"
        return (float(k1) if k1 else None, float(k2) if k2 else None, blocked)

    def dota_events(self) -> list[PariEvent]:
        children: dict[int, list[dict]] = {}
        for e in self.events.values():
            if e.get("parentId") in self.events:
                children.setdefault(e["parentId"], []).append(e)
        out = []
        for e in self.events.values():
            if e.get("parentId") in self.events or not e.get("team1") or not e.get("team2"):
                continue
            segment = self._segment_name(e.get("sportId"))
            k1, k2, blocked = self._odds(e["id"])
            maps = []
            for ch in sorted(children.get(e["id"], []), key=lambda c: c.get("sortOrder") or ""):
                c1, c2, cb = self._odds(ch["id"])
                if c1 or c2:
                    maps.append(PariMarket(ch["id"], ch.get("name") or "", c1, c2, cb))
            out.append(PariEvent(
                event_id=e["id"], segment=segment, team1=e["team1"], team2=e["team2"],
                start_time=e.get("startTime"), place=e.get("place") or "line", k1=k1, k2=k2,
                blocked=blocked, mixer=bool(MIXER_RE.search(segment)), maps=maps))
        return sorted(out, key=lambda ev: (not ev.mixer, ev.start_time or 0))

    def status(self) -> dict:
        return {"host": self.host, "version": self.version, "events": len(self.events),
                "fetched_at": int(self.fetched_at) or None, "error": self.last_error}


# --- сопоставление команд ------------------------------------------------------

# Кириллица, похожая на латиницу: в никах их смешивают, у букмекера могут написать иначе.
_HOMOGLYPHS = str.maketrans("аеорсхукмтвніё", "aeopcxykmtbhie")
_PREFIX_RE = re.compile(r"^(team|команда|тим)[\s._-]+", re.I)


def normalize_team(name: str) -> str:
    s = _PREFIX_RE.sub("", name.strip().lower())
    s = s.translate(_HOMOGLYPHS)
    return re.sub(r"[^0-9a-zа-я]+", "", s)


def team_letter(name: str) -> int | None:
    """«Команда A» / «Team B» → номер команды (1 = A), как на сайте mixer-cup."""
    s = normalize_team(name)
    if len(s) == 1 and "a" <= s <= "z" and _PREFIX_RE.match(name.strip()):
        return ord(s) - ord("a") + 1
    return None


def best_team(name: str, teams: dict[str, str], threshold: float = 0.8,
              numbers: dict[str, int | None] | None = None) -> tuple[str | None, float]:
    """teams: ключ → имя, numbers: ключ → номер команды. Возвращает (ключ, похожесть)."""
    target = normalize_team(name)
    if not target:
        return None, 0.0
    exact = next((k for k, n in teams.items() if normalize_team(n) == target), None)
    if exact is not None:
        return exact, 1.0
    letter = team_letter(name)
    if letter is not None and numbers:
        key = next((k for k, n in numbers.items() if n == letter), None)
        return (key, 1.0) if key else (None, 0.0)
    best, score = None, 0.0
    for key, team_name in teams.items():
        cand = normalize_team(team_name)
        if not cand:
            continue
        s = 1.0 if cand == target else difflib.SequenceMatcher(None, cand, target).ratio()
        if s > score:
            best, score = key, s
    return (best, score) if score >= threshold else (None, score)


def link_events(events: list[PariEvent], teams: dict[str, str],
                numbers: dict[str, int | None] | None = None) -> list[dict]:
    out = []
    for ev in events:
        k1, s1 = best_team(ev.team1, teams, numbers=numbers)
        k2, s2 = best_team(ev.team2, teams, numbers=numbers)
        linked = k1 is not None and k2 is not None and k1 != k2
        out.append({**ev.as_dict(), "team1_key": k1 if linked else None,
                    "team2_key": k2 if linked else None, "linked": linked,
                    "match_score": round(min(s1, s2), 3)})
    return out
