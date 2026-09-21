"""
Central config. Secrets come from environment variables / .env (per the
project's decision to avoid a keychain/vault dependency for v1). Each
connector plugin defines and documents its own required env vars in its
own package -- this file only holds settings the core needs.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ADD_IT_ALL_", env_file=".env", extra="ignore")

    cache_db_path: Path = Path.home() / ".add-it-all" / "cache.sqlite3"
    cache_ttl_seconds: int = 3600

    @field_validator("cache_db_path", mode="after")
    @classmethod
    def _expand_user(cls, value: Path) -> Path:
        """Expand a leading `~`.

        `.env.example` ships `~/.add-it-all/cache.sqlite3` and pydantic
        coerces that to Path("~/...") literally. Without this, the cache --
        plaintext employee PII -- is created in a directory named `~` under
        the current working directory, which for a dev running from the repo
        means inside the git checkout.
        """
        return value.expanduser()


#: Owner-only: the cache directory holds employee PII in plaintext.
CACHE_DIR_MODE = 0o700


def get_settings() -> Settings:
    settings = Settings()
    cache_dir = settings.cache_db_path.parent
    cache_dir.mkdir(parents=True, exist_ok=True, mode=CACHE_DIR_MODE)
    # `mode` is masked by umask and ignored entirely when the directory
    # already exists, so pin the permissions explicitly.
    cache_dir.chmod(CACHE_DIR_MODE)
    return settings
