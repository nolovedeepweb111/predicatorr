from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from predicator.config import Settings  # noqa: E402
from predicator.db import connect  # noqa: E402
from predicator.importer import bump_data_version, import_backup  # noqa: E402
from synthetic import make_backup  # noqa: E402


@pytest.fixture(scope="session")
def backup() -> dict:
    return make_backup()


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return replace(Settings(), db_path=tmp_path / "test.sqlite3", pari_hosts=("http://127.0.0.1:9",),
                   mixer_live=False, sync_minutes=0, password="")


@pytest.fixture()
def conn(settings, backup):
    c = connect(settings.db_path)
    import_backup(c, backup)
    bump_data_version(c)
    yield c
    c.close()
