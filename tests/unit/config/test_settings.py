"""
Stage 1: `Settings` config loader.

Stage 2 (Okta) is the first stage to depend on this for real credentials,
so the loader's precedence rules are pinned down here first.

Run just this stage:  pytest -m cache
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from add_it_all.config import Settings, get_settings

pytestmark = pytest.mark.cache

# Every env var that could leak in from the developer's real shell or from
# the repo's own .env and quietly invalidate a precedence assertion.
_INTERFERING_VARS = (
    "ADD_IT_ALL_CACHE_DB_PATH",
    "ADD_IT_ALL_CACHE_TTL_SECONDS",
)


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """Isolate Settings from the developer's shell AND from the repo's .env.

    `Settings` reads `.env` relative to the current working directory, so a
    test that does not chdir would pick up the real repo .env once bootstrap
    has created one -- making these assertions pass or fail depending on
    whether the developer had run bootstrap. chdir to an empty tmp_path.
    """
    for var in _INTERFERING_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_dotenv(directory: Path, body: str) -> None:
    (directory / ".env").write_text(body, encoding="utf-8")


def test_defaults_apply_when_nothing_is_configured(clean_env):
    settings = Settings()
    assert settings.cache_ttl_seconds == 3600
    assert settings.cache_db_path == Path.home() / ".add-it-all" / "cache.sqlite3"


def test_bare_settings_never_raises_on_empty_environment(clean_env):
    """No field is required today; adding a required one must break this test."""
    Settings()  # must not raise


def test_env_var_overrides_default(clean_env, monkeypatch):
    monkeypatch.setenv("ADD_IT_ALL_CACHE_TTL_SECONDS", "60")
    assert Settings().cache_ttl_seconds == 60


def test_dotenv_is_used_when_env_var_absent(clean_env):
    _write_dotenv(clean_env, "ADD_IT_ALL_CACHE_TTL_SECONDS=120\n")
    assert Settings().cache_ttl_seconds == 120


def test_env_var_takes_precedence_over_dotenv(clean_env, monkeypatch):
    _write_dotenv(clean_env, "ADD_IT_ALL_CACHE_TTL_SECONDS=120\n")
    monkeypatch.setenv("ADD_IT_ALL_CACHE_TTL_SECONDS", "60")
    assert Settings().cache_ttl_seconds == 60


def test_unprefixed_service_credentials_are_ignored_not_fatal(clean_env, monkeypatch):
    """Per-plugin vars (OKTA_*, JIRA_*) live outside core's env_prefix.

    They must neither crash core config nor get absorbed onto Settings.
    """
    monkeypatch.setenv("OKTA_API_TOKEN", "not-a-real-token")
    monkeypatch.setenv("JIRA_EMAIL", "jdoe@example.com")

    settings = Settings()

    assert not hasattr(settings, "okta_api_token")
    assert "not-a-real-token" not in settings.model_dump_json()


def test_path_is_coerced_from_string(clean_env, monkeypatch):
    monkeypatch.setenv("ADD_IT_ALL_CACHE_DB_PATH", str(clean_env / "custom" / "c.sqlite3"))
    assert Settings().cache_db_path == clean_env / "custom" / "c.sqlite3"


def test_tilde_in_cache_path_is_expanded(clean_env, monkeypatch):
    """`.env.example` ships `~/.add-it-all/cache.sqlite3`.

    Pydantic coerces that string to Path("~/...") verbatim, so without
    expansion the cache is created in a directory literally named `~`
    under the current working directory -- i.e. a plaintext store of
    employee PII inside the git checkout, not in $HOME.
    """
    monkeypatch.setenv("ADD_IT_ALL_CACHE_DB_PATH", "~/.add-it-all/cache.sqlite3")

    resolved = Settings().cache_db_path

    assert "~" not in str(resolved)
    assert resolved == Path.home() / ".add-it-all" / "cache.sqlite3"


def test_tilde_from_dotenv_is_expanded_too(clean_env):
    _write_dotenv(clean_env, "ADD_IT_ALL_CACHE_DB_PATH=~/.add-it-all/cache.sqlite3\n")
    assert Settings().cache_db_path == Path.home() / ".add-it-all" / "cache.sqlite3"


def test_get_settings_does_not_create_a_tilde_directory(clean_env, monkeypatch):
    monkeypatch.setenv("ADD_IT_ALL_CACHE_DB_PATH", str(clean_env / "ok" / "cache.sqlite3"))
    get_settings()
    assert not (clean_env / "~").exists()


def test_invalid_ttl_raises_a_validation_error(clean_env, monkeypatch):
    monkeypatch.setenv("ADD_IT_ALL_CACHE_TTL_SECONDS", "not-a-number")
    with pytest.raises(Exception):  # pydantic ValidationError
        Settings()


def test_get_settings_creates_the_cache_directory(clean_env, monkeypatch):
    target = clean_env / "nested" / "dir" / "cache.sqlite3"
    monkeypatch.setenv("ADD_IT_ALL_CACHE_DB_PATH", str(target))

    settings = get_settings()

    assert target.parent.is_dir()
    assert settings.cache_db_path == target


def test_get_settings_creates_cache_directory_owner_only(clean_env, monkeypatch):
    """The cache holds employee PII -- its directory must not be world-readable."""
    target = clean_env / "private" / "cache.sqlite3"
    monkeypatch.setenv("ADD_IT_ALL_CACHE_DB_PATH", str(target))

    get_settings()

    mode = stat.S_IMODE(target.parent.stat().st_mode)
    assert mode == 0o700, f"expected 0o700, got {mode:#o}"


def test_get_settings_is_idempotent_on_existing_directory(clean_env, monkeypatch):
    target = clean_env / "existing" / "cache.sqlite3"
    target.parent.mkdir(parents=True)
    monkeypatch.setenv("ADD_IT_ALL_CACHE_DB_PATH", str(target))

    get_settings()
    get_settings()  # must not raise on the second call
