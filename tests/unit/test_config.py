"""Tests for the Settings model: env prefix, legacy aliases, precedence,
coercions, secrets, and the --upload cross-field rule."""

from __future__ import annotations

import os

import pytest
from pydantic import SecretStr, ValidationError

from breezeai_cog.config import Settings

_LEGACY = [
    "BREEZE_API_URL",
    "API_KEY",
    "AWS_ACCESS_KEY",
    "AWS_ACCESS_KEYID",
    "AWS_SECRET_KEY",
    "AWS_REGION",
    "AWS_S3_BUCKET",
]


@pytest.fixture
def env(monkeypatch):
    """Isolate from the ambient environment so defaults are deterministic."""
    for key in list(os.environ):
        if key.startswith("BREEZEAI_COG_") or key in _LEGACY:
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_defaults(env) -> None:
    s = _settings()
    assert s.repo is None and s.languages is None and s.jobs is None
    assert s.capture_statements is False and s.upload is False
    assert s.log_level == "INFO" and s.log_format == "plaintext" and s.log_to_file is True
    assert s.port == 3000
    assert s.text_truncation_limit == 8000 and s.max_file_size == 2_000_000
    assert s.aws_region == "us-west-2"


def test_env_prefix(env) -> None:
    env.setenv("BREEZEAI_COG_CAPTURE_STATEMENTS", "true")
    env.setenv("BREEZEAI_COG_JOBS", "8")
    env.setenv("BREEZEAI_COG_PORT", "9000")
    s = _settings()
    assert s.capture_statements is True and s.jobs == 8 and s.port == 9000


def test_legacy_aliases(env) -> None:
    env.setenv("BREEZE_API_URL", "https://api.breeze.ai")
    env.setenv("API_KEY", "k-123")
    env.setenv("AWS_ACCESS_KEYID", "AKIA...")  # legacy spelling of the access key
    env.setenv("AWS_SECRET_KEY", "shh")
    env.setenv("AWS_S3_BUCKET", "my-bucket")
    s = _settings()
    assert s.baseurl == "https://api.breeze.ai"
    assert s.user_api_key.get_secret_value() == "k-123"
    assert s.aws_access_key == "AKIA..."
    assert s.aws_secret_key.get_secret_value() == "shh"
    assert s.aws_s3_bucket == "my-bucket"


def test_init_overrides_env(env) -> None:
    env.setenv("BREEZEAI_COG_PORT", "9000")
    assert _settings(port=5000).port == 5000  # explicit input wins over env


def test_languages_comma_split(env) -> None:
    assert _settings(languages="typescript, python").languages == ["typescript", "python"]
    env.setenv("BREEZEAI_COG_LANGUAGE", "go,java")
    assert _settings().languages == ["go", "java"]


def test_log_level_normalization(env) -> None:
    assert _settings(log_level="warn").log_level == "WARNING"
    assert _settings(log_level="debug").log_level == "DEBUG"
    with pytest.raises(ValidationError):
        _settings(log_level="bogus")


def test_upload_requires_fields(env) -> None:
    with pytest.raises(ValidationError) as exc:
        _settings(upload=True)
    msg = str(exc.value)
    assert "baseurl" in msg and "uuid" in msg and "user_api_key" in msg
    # complete set validates
    ok = _settings(upload=True, baseurl="https://x", uuid="u", user_api_key="k")
    assert ok.upload is True


def test_secret_not_leaked(env) -> None:
    s = _settings(user_api_key="topsecret")
    assert isinstance(s.user_api_key, SecretStr)
    assert "topsecret" not in repr(s)
    assert s.user_api_key.get_secret_value() == "topsecret"
