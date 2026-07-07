"""Configuration — a single ``Settings`` model (the one source of config).

Every CLI flag maps 1:1 to a field here and to an environment variable (and
``.env``). Precedence (highest first), per ARCHITECTURE.md §8::

    explicit input (CLI flags │ request query params)  >  env vars  >  .env  >  defaults

pydantic-settings already ranks init kwargs above env/.env, so the CLI layer
constructs ``Settings(**explicitly_provided_flags)`` (omitting unset flags so env
can supply them), and the server builds a per-request copy via
``base.model_copy(update=whitelisted_overrides)``. Settings are **injected** into
services — there is no module-level singleton.

App options use the ``BREEZEAI_COG_*`` env prefix; well-known integration vars keep
their conventional names as aliases for drop-in compatibility with the current
deployment (``BREEZE_API_URL``, ``API_KEY``, ``AWS_*``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import (
    AliasChoices,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class Settings(BaseSettings):
    """Resolved configuration for a CLI run or a server request."""

    model_config = SettingsConfigDict(
        env_prefix="BREEZEAI_COG_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # ── Analysis ──────────────────────────────────────────────────────────
    repo: Path | None = None  # --repo
    out: Path | None = None  # --out; output DIRECTORY (filename is derived)
    languages: list[str] | None = Field(  # --language; None = auto-detect all
        default=None,
        validation_alias=AliasChoices("BREEZEAI_COG_LANGUAGE", "BREEZEAI_COG_LANGUAGES"),
    )
    capture_statements: bool = False  # --capture-statements
    jobs: int | None = None  # --jobs; None = CPU count (resolved in the executor)
    text_truncation_limit: int = 8000  # max captured statement `text` length (utils/text.py)
    max_file_size: int = 2_000_000  # bytes; scanner skips larger files (core/scanner.py)
    parse_timeout: float = 10.0  # seconds; per-file tree-sitter native timeout (0 disables)

    # ── Logging (see §11) ─────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: Literal["plaintext", "json"] = "plaintext"
    log_to_file: bool = True
    log_location: Path = Path("./logs")

    # ── Server ────────────────────────────────────────────────────────────
    port: int = 3000

    # ── Backend upload ────────────────────────────────────────────────────
    upload: bool = False  # --upload toggle
    baseurl: str | None = Field(
        default=None,
        validation_alias=AliasChoices("BREEZEAI_COG_BASEURL", "BREEZE_API_URL"),
    )
    uuid: str | None = None
    user_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("BREEZEAI_COG_USER_API_KEY", "API_KEY"),
    )

    # ── AWS / S3 (server, conventional unprefixed names) ──────────────────
    aws_access_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AWS_ACCESS_KEY", "AWS_ACCESS_KEYID"),
    )
    aws_secret_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("AWS_SECRET_KEY")
    )
    aws_region: str = Field(default="us-west-2", validation_alias=AliasChoices("AWS_REGION"))
    aws_s3_bucket: str | None = Field(
        default=None, validation_alias=AliasChoices("AWS_S3_BUCKET")
    )

    # ── Validators ────────────────────────────────────────────────────────
    @field_validator("languages", mode="before")
    @classmethod
    def _split_languages(cls, v: object) -> object:
        """Accept a comma-separated string (CLI / env) as a list."""
        if isinstance(v, str):
            return [part.strip() for part in v.split(",") if part.strip()]
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, v: object) -> object:
        if isinstance(v, str):
            level = v.strip().upper()
            level = {"WARN": "WARNING", "FATAL": "CRITICAL"}.get(level, level)
            if level not in _LOG_LEVELS:
                raise ValueError(
                    f"log_level must be one of {sorted(_LOG_LEVELS)} (got {v!r})"
                )
            return level
        return v

    @model_validator(mode="after")
    def _check_upload_requirements(self) -> "Settings":
        """`--upload` requires baseurl + uuid + user_api_key (spec A1)."""
        if self.upload:
            missing = [
                name
                for name, value in (
                    ("baseurl", self.baseurl),
                    ("uuid", self.uuid),
                    ("user_api_key", self.user_api_key),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "--upload requires " + ", ".join(missing) + " to be set"
                )
        return self
