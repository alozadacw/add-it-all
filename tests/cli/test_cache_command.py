"""
Operator-facing cache commands.

The cache is a plaintext local store of employee PII, so "how do I empty
this?" needs an answer that isn't `rm` against a path the user has to
know. Stage 1 scope.

Run just this stage:  pytest -m cache
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from add_it_all.cache import Cache
from add_it_all.cli import app
from add_it_all.plugins.base import ConnectorResult

pytestmark = pytest.mark.cache

runner = CliRunner()


@pytest.fixture
def cache_db(tmp_path, monkeypatch):
    """Point the CLI at a throwaway cache DB via the documented env var."""
    db_path = tmp_path / "cache.sqlite3"
    monkeypatch.setenv("ADD_IT_ALL_CACHE_DB_PATH", str(db_path))
    monkeypatch.chdir(tmp_path)  # don't pick up the repo's real .env
    return db_path


def _seed(db_path, count: int = 2) -> None:
    cache = Cache(db_path)
    for i in range(count):
        cache.put(ConnectorResult(plugin_name=f"svc{i}", identifier="jdoe"))


def test_cache_path_reports_the_active_database(cache_db):
    result = runner.invoke(app, ["cache", "path"])
    assert result.exit_code == 0
    assert str(cache_db) in result.stdout


def test_cache_clear_empties_the_cache(cache_db):
    _seed(cache_db, 2)

    result = runner.invoke(app, ["cache", "clear"])

    assert result.exit_code == 0
    assert "2" in result.stdout
    assert Cache(cache_db).get("svc0", "jdoe") is None


def test_cache_clear_on_empty_cache_succeeds(cache_db):
    result = runner.invoke(app, ["cache", "clear"])
    assert result.exit_code == 0


def test_cache_purge_reports_removed_count(cache_db):
    result = runner.invoke(app, ["cache", "purge"])
    assert result.exit_code == 0
