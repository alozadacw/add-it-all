"""
Stage 1 acceptance tests: cache & data model.

Run just this stage:  pytest -m cache
"""

import sqlite3
import stat
from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time

from add_it_all.cache import Cache
from add_it_all.models import UnifiedRecord
from add_it_all.plugins.base import ConnectorResult

pytestmark = pytest.mark.cache


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path / "test_cache.sqlite3", default_ttl=timedelta(hours=1))


def _result(plugin="okta", identifier="jdoe", **kwargs) -> ConnectorResult:
    return ConnectorResult(plugin_name=plugin, identifier=identifier, **kwargs)


def test_put_then_get_round_trip(cache):
    cache.put(_result(data={"status": "active"}))
    fetched = cache.get("okta", "jdoe")
    assert fetched is not None
    assert fetched.data == {"status": "active"}


def test_get_missing_key_returns_none(cache):
    assert cache.get("okta", "nobody") is None


def test_entry_expires_after_ttl(cache):
    with freeze_time("2026-01-01T00:00:00+00:00"):
        cache.put(_result(data={"status": "active"}))

    with freeze_time("2026-01-01T02:00:00+00:00"):  # 2h later, ttl=1h
        assert cache.get("okta", "jdoe") is None


def test_entry_within_ttl_is_returned(cache):
    with freeze_time("2026-01-01T00:00:00+00:00"):
        cache.put(_result(data={"status": "active"}))

    with freeze_time("2026-01-01T00:30:00+00:00"):  # 30m later, ttl=1h
        assert cache.get("okta", "jdoe") is not None


def test_invalidate_removes_entry(cache):
    cache.put(_result(data={"status": "active"}))
    cache.invalidate("okta", "jdoe")
    assert cache.get("okta", "jdoe") is None


def test_different_plugins_same_identifier_cached_independently(cache):
    cache.put(_result(plugin="okta", identifier="jdoe", data={"status": "active"}))
    cache.put(_result(plugin="jira", identifier="jdoe", data={"tickets": []}))
    assert cache.get("okta", "jdoe").data == {"status": "active"}
    assert cache.get("jira", "jdoe").data == {"tickets": []}


# --- Retention -----------------------------------------------------------------
#
# The cache accumulates employee PII (Okta status, device serials, ticket
# history) in plaintext SQLite. Expiry must actually delete rows, and there
# must be an operator-facing way to empty it -- a TTL that only hides rows
# from reads leaves the data on disk indefinitely.


def _row_count(db_path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
    finally:
        conn.close()


def test_expired_entry_is_deleted_from_storage_not_merely_hidden(cache):
    with freeze_time("2026-01-01T00:00:00+00:00"):
        cache.put(_result(data={"status": "active"}))
        assert _row_count(cache.db_path) == 1

    with freeze_time("2026-01-01T02:00:00+00:00"):  # 2h later, ttl=1h
        assert cache.get("okta", "jdoe") is None

    assert _row_count(cache.db_path) == 0, "expired PII must not linger on disk"


def test_purge_expired_removes_only_expired_rows_and_reports_count(cache):
    with freeze_time("2026-01-01T00:00:00+00:00"):
        cache.put(_result(plugin="okta", data={"status": "active"}))

    with freeze_time("2026-01-01T01:30:00+00:00"):
        cache.put(_result(plugin="jira", data={"tickets": []}))

        removed = cache.purge_expired()  # okta is 1.5h old, jira is fresh

        assert removed == 1
        assert _row_count(cache.db_path) == 1
        # Must stay inside the frozen clock: at real "now" the jira entry is
        # also long expired, and reading it would delete it.
        assert cache.get("jira", "jdoe") is not None


def test_purge_expired_on_empty_cache_returns_zero(cache):
    assert cache.purge_expired() == 0


def test_clear_removes_everything_and_reports_count(cache):
    cache.put(_result(plugin="okta", data={"status": "active"}))
    cache.put(_result(plugin="jira", data={"tickets": []}))

    removed = cache.clear()

    assert removed == 2
    assert _row_count(cache.db_path) == 0


def test_cache_creates_missing_parent_directory(tmp_path):
    db_path = tmp_path / "does" / "not" / "exist" / "cache.sqlite3"
    Cache(db_path)
    assert db_path.exists()


def test_cache_directory_is_owner_only(tmp_path):
    db_path = tmp_path / "private" / "cache.sqlite3"
    Cache(db_path)
    mode = stat.S_IMODE(db_path.parent.stat().st_mode)
    assert mode == 0o700, f"expected 0o700, got {mode:#o}"


def test_cache_file_is_owner_only(tmp_path):
    db_path = tmp_path / "cache.sqlite3"
    Cache(db_path)
    mode = stat.S_IMODE(db_path.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {mode:#o}"


def test_unified_record_merges_multiple_plugin_results():
    okta_result = _result(plugin="okta", data={"status": "active"})
    jamf_result = _result(plugin="jamf", data={}, error="timeout")

    record = UnifiedRecord.from_results("jdoe", [okta_result, jamf_result])

    assert record.field_for("okta") == {"status": "active"}
    assert record.field_for("jamf") is None  # errored -> degrades gracefully
    assert record.errors() == {"jamf": "timeout"}


def test_unified_record_missing_plugin_returns_none_not_exception():
    record = UnifiedRecord.from_results("jdoe", [])
    assert record.field_for("okta") is None
    assert record.errors() == {}
