"""Минимальный HTTP-клиент на stdlib: JSON по GET/POST, gzip, понятные ошибки."""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.request
from typing import Any

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"


class FetchError(RuntimeError):
    pass


def _read(req: urllib.request.Request, timeout: float) -> bytes:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300].decode("utf-8", "replace")
        raise FetchError(f"{req.full_url}: HTTP {exc.code} {body}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"{req.full_url}: {exc}") from exc


def get_bytes(url: str, headers: dict[str, str] | None = None, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept-Encoding": "gzip", **(headers or {})})
    return _read(req, timeout)


def get_json(url: str, headers: dict[str, str] | None = None, timeout: float = 30) -> Any:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip",
        **(headers or {})})
    data = _read(req, timeout)
    try:
        return json.loads(data)
    except ValueError as exc:
        raise FetchError(f"{url}: ответ не JSON ({data[:120]!r})") from exc


def post_json(url: str, payload: Any, headers: dict[str, str] | None = None,
              timeout: float = 30) -> Any:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json",
                 "Accept": "application/json", "Accept-Encoding": "gzip", **(headers or {})})
    data = _read(req, timeout)
    try:
        return json.loads(data)
    except ValueError as exc:
        raise FetchError(f"{url}: ответ не JSON ({data[:120]!r})") from exc
