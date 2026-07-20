"""Configuration loader.

Single source of truth for runtime settings. Reads from environment / `.env`,
validates with pydantic, and exposes a frozen `settings` object. Importing
anything else should never read env vars directly — they come through here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- SEC EDGAR ---
    sec_user_agent: str = Field(
        default="AI-Investment-OS admin@example.com",
        description="MANDATORY. SEC requires a User-Agent identifying you.",
    )

    # --- FRED ---
    fred_api_key: str = Field(default="")

    # --- SimFin (optional) ---
    simfin_api_key: str = Field(default="")

    # --- Tiingo EOD prices (optional; each user supplies their own token) ---
    tiingo_api_key: str = Field(default="")

    # --- LLM (reserved, unused in foundation phase) ---
    anthropic_api_key: str = Field(default="")
    zai_api_key: str = Field(default="")

    # --- Paths ---
    duckdb_path: Path = Field(default=Path("data/aios.duckdb"))
    parquet_dir: Path = Field(default=Path("data/parquet"))
    log_dir: Path = Field(default=Path("logs"))

    # --- Ingest tuning ---
    edgar_max_rps: int = Field(default=8, description="SEC fair-access limit is 10; we stay under.")
    yfinance_sleep_sec: float = Field(default=0.5)

    @computed_field  # type: ignore[prop-defined]
    @property
    def project_root(self) -> Path:
        """Resolve project root as the parent of the `src/` directory."""
        # This file lives at src/aios/config.py → root is two parents up.
        return Path(__file__).resolve().parents[2]

    def ensure_dirs(self) -> None:
        """Create data/log directories if missing. Call at startup."""
        for p in (
            self.duckdb_path.parent,
            self.parquet_dir,
            self.log_dir,
        ):
            p_abs = p if p.is_absolute() else self.project_root / p
            p_abs.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton."""
    s = Settings()
    s.ensure_dirs()
    return s


# Convenience module-level access
settings = get_settings()
