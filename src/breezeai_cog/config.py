"""Configuration — a single ``Settings`` model (the one source of config).

Every CLI flag maps 1:1 to a field here and to an environment variable (and
``.env``). Precedence (highest first)::

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
    statement_text_limit: int = 8000  # max captured code statement `text`/body length (utils/text.py)
    metadata_value_limit: int = 4000  # max structured-JSON leaf value length on File.metadata
    #: (structured_json/parser.py); 0 disables truncation.
    max_file_size: int = 2_000_000  # bytes; scanner skips larger files (core/scanner.py)
    parse_timeout: float = 10.0  # seconds; per-file tree-sitter native timeout (0 disables)
    # --max-concat-depth; max `+` nesting folded into an endpoint before bailing to null.
    # Guards against RecursionError on generated HTML/JS string builders. Keep modest — a
    # real URL concat is <10 parts, and very high values can re-trigger the recursion the
    # cap prevents (those files are then dropped with a warning). (parsers/statements_common)
    max_concat_depth: int = Field(default=100, ge=1)

    # ── Logging ─────────────────────────────────────────────────
    log_level: str = "INFO"
    log_format: Literal["plaintext", "json"] = "plaintext"
    log_to_file: bool = True
    # None = auto: the CLI resolves it to `<repo>/.cog/logs`; the server falls back to
    # `./logs`. An explicit BREEZEAI_COG_LOG_LOCATION (env/.env) always wins.
    log_location: Path | None = None

    # ── Server ────────────────────────────────────────────────────────────
    port: int = 3000

    # ── Git ───────────────────────────────────────────────────────────────
    git_clone_timeout: float = 1800.0  # seconds; git clone/fetch/checkout cap (server/git.py)

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
    # Per-repo upload POST cap (seconds); the request fails/retries past this. Default 15 min —
    # large ontologies stream over a single request (services/upload.py). --upload-timeout.
    upload_timeout: float = Field(default=900.0, gt=0)
    # Concurrent uploads in --batch mode. Default 1 (serial). --parallel-uploads.
    upload_parallelism: int = Field(default=1, ge=1)
    # Retries after a failed upload (total attempts = upload_max_retries + 1). Only transient
    # failures (network / timeout / HTTP 5xx) retry; a 4xx is fatal. --upload-max-retries.
    upload_max_retries: int = Field(default=1, ge=0)

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

    @property
    def aws_credentials_kwargs(self) -> dict[str, str]:
        """Static AWS credential kwargs for boto3 clients, when configured.

        Returns the ``aws_access_key_id`` / ``aws_secret_access_key`` pair only
        when both are set, so callers can splat it into ``boto3.client(...)``.
        When empty, boto3 falls back to its default provider chain — IAM Roles
        for Service Accounts (IRSA) in-cluster, or the local AWS profile for dev.
        """
        if self.aws_access_key and self.aws_secret_key:
            return {
                "aws_access_key_id": self.aws_access_key,
                "aws_secret_access_key": self.aws_secret_key.get_secret_value(),
            }
        return {}

    @model_validator(mode="after")
    def _check_upload_requirements(self) -> "Settings":
        """`--upload` requires baseurl + uuid + user_api_key."""
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
