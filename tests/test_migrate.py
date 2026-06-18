"""Tests for the standalone migration entrypoint."""

import pytest

import scripts.migrate as migrate


class _FakeSettings:
    def __init__(self, url):
        self.database_url = url


def test_main_calls_init_db_with_settings_url(monkeypatch):
    captured = {}

    async def fake_init_db(url):
        captured["url"] = url

    monkeypatch.setattr(
        migrate,
        "get_settings",
        lambda: _FakeSettings("postgresql+asyncpg://baloo:pw@/baloo?host=/cloudsql/p:r:i"),
    )
    monkeypatch.setattr(migrate, "init_db", fake_init_db)

    migrate.main()

    assert captured["url"].startswith("postgresql+asyncpg://")
    assert "host=/cloudsql/" in captured["url"]


def test_main_errors_when_database_url_empty(monkeypatch):
    monkeypatch.setattr(migrate, "get_settings", lambda: _FakeSettings(""))
    monkeypatch.setattr(migrate, "init_db", lambda url: None)

    with pytest.raises(SystemExit):
        migrate.main()
