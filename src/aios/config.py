"""Configuration loader.

Single source of truth for runtime settings. Reads from environment / `.env`,
validates with pydantic, and exposes a frozen `settings` object. Importing
anything else should never read env vars directly — they come through here.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aios.security import ensure_private_directory


def secret_value(value: SecretStr | str) -> str:
    """Reveal a configured secret only at the provider boundary.

    The ``str`` branch keeps narrow test doubles and downstream integrations
    compatible while the real Settings model stores credentials as SecretStr.
    """

    return value.get_secret_value() if isinstance(value, SecretStr) else value


def _looks_like_project_root(candidate: Path) -> bool:
    return (candidate / "pyproject.toml").is_file() and (
        (candidate / "src" / "aios").is_dir() or (candidate / "data").is_dir()
    )


def _discover_project_root() -> Path:
    """Find a checkout root without binding an installed wheel to site-packages."""
    working_directory = Path.cwd().resolve()
    for candidate in (working_directory, *working_directory.parents):
        if _looks_like_project_root(candidate):
            return candidate

    source_checkout = Path(__file__).resolve().parents[2]
    if _looks_like_project_root(source_checkout):
        return source_checkout

    raise ValueError(
        "AIOS project root could not be discovered; run from the AIOS workspace "
        "or set AIOS_PROJECT_ROOT to its existing directory"
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- SEC EDGAR ---
    sec_user_agent: str = Field(
        default="AI-Investment-OS admin@example.com",
        description="MANDATORY. SEC requires a User-Agent identifying you.",
    )

    # --- FRED ---
    fred_api_key: SecretStr = Field(default=SecretStr(""))

    # --- SimFin (optional) ---
    simfin_api_key: SecretStr = Field(default=SecretStr(""))

    # --- Tiingo EOD prices (optional; each user supplies their own token) ---
    tiingo_api_key: SecretStr = Field(default=SecretStr(""))

    # --- LLM (reserved, unused in foundation phase) ---
    anthropic_api_key: SecretStr = Field(default=SecretStr(""))
    zai_api_key: SecretStr = Field(default=SecretStr(""))

    # --- External email alerts (optional; disabled until explicitly activated) ---
    smtp_host: str = Field(default="")
    # Keep optional email numerics as raw strings so a typo cannot break core
    # research/health commands; the email boundary validates them on use.
    smtp_port: str = Field(default="587")
    smtp_security: str = Field(default="starttls")
    smtp_username: str = Field(default="")
    smtp_password: SecretStr = Field(default=SecretStr(""))
    alert_email_from: str = Field(default="")
    alert_email_to: str = Field(default="")
    smtp_timeout_seconds: str = Field(default="15")

    # --- Paths ---
    project_root: Path = Field(
        default_factory=_discover_project_root,
        validation_alias="AIOS_PROJECT_ROOT",
    )
    duckdb_path: Path = Field(default=Path("data/aios.duckdb"))
    operations_db_path: Path = Field(default=Path("data/operations/alerts.sqlite3"))
    raw_data_dir: Path = Field(default=Path("data/raw"))
    parquet_dir: Path = Field(default=Path("data/parquet"))
    log_dir: Path = Field(default=Path("logs"))

    # --- Ingest tuning ---
    edgar_max_rps: int = Field(default=8, description="SEC fair-access limit is 10; we stay under.")
    yfinance_sleep_sec: float = Field(default=0.5)
    yfinance_max_attempts: int = Field(default=3, ge=1, le=5)
    yfinance_retry_base_sec: float = Field(default=1.0, ge=0.0, le=30.0)
    duckdb_lock_wait_seconds: float = Field(
        default=10.0,
        ge=0.0,
        le=600.0,
        description="Bounded wait when another local AIOS process briefly owns DuckDB.",
    )

    @field_validator("project_root")
    @classmethod
    def _resolve_project_root(cls, value: Path) -> Path:
        root = Path(value).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        root = root.resolve()
        if not root.is_dir():
            raise ValueError(f"AIOS_PROJECT_ROOT is not an existing directory: {root}")
        return root

    def ensure_dirs(self) -> None:
        """Create data/log directories if missing. Call at startup."""
        root = self.project_root.resolve()
        for p in (
            self.duckdb_path.parent,
            self.operations_db_path.parent,
            self.raw_data_dir,
            self.parquet_dir,
            self.log_dir,
        ):
            requested = p if p.is_absolute() else root / p
            destination = Path(os.path.abspath(requested))
            if destination == root or root not in destination.parents:
                raise ValueError(
                    "AIOS runtime directories must be dedicated children of "
                    f"the project root: {destination}"
                )
            ensure_private_directory(destination)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton."""
    s = Settings()
    s.ensure_dirs()
    return s


# Convenience module-level access
settings = get_settings()
