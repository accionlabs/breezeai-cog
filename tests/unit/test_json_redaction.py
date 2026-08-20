"""Layer-2 value redaction for structured-JSON capture.

The governing constraint is **no false positives**: benign values that merely *look*
random (hashes, UUIDs, git SHAs, base64 assets, plain URLs, version strings) must pass
through untouched, while structurally unambiguous secrets are redacted. The `KEEP` corpus
is therefore as important as the `REDACT` one.
"""

from __future__ import annotations

import json

import pytest

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.structured_json.parser import StructuredJsonParser
from breezeai_cog.parsers.structured_json.redaction import redact_secrets

# ── benign values that MUST survive verbatim (false-positive guard) ─────────────
KEEP = [
    "d41d8cd98f00b204e9800998ecf8427e",                                  # md5 hex
    "da39a3ee5e6b4b0d3255bfef95601890afd80709",                          # sha1 / git sha
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # sha256 hex
    "550e8400-e29b-41d4-a716-446655440000",                             # UUID
    "f3347b4",                                                           # short git sha
    "iVBORw0KGgoAAAANSUhEUgAAAAUAAAAFCAYAAAC",                          # base64 PNG asset
    "https://api.example.com/v1/users?page=2&sort=name",                # plain URL, no creds
    "http://localhost:8080/health",                                     # URL with host:port
    "redis://cache.internal:6379/0",                                    # URL host:port, no @
    "user@example.com",                                                  # email
    "1.2.3",                                                             # version
    "2.14.0-rc.1+build.456",                                            # semver with build
    "AKIABANK",                                                          # AKIA-ish but too short
    "AKIA",                                                              # bare prefix
    "eyJhbGciOiJIUzI1NiJ9",                                             # ONE base64 segment, not a JWT
    "/usr/local/bin/python3.13",                                        # path
    "The quick brown fox jumps over the lazy dog",                     # prose
    "template-mapping-00-01-03",                                        # dashed code
    "",                                                                  # empty
]

# ── (input, expected) for values that MUST be redacted ──────────────────────────
GH = "ghp_" + "a" * 36
PAT = "github_pat_" + "B" * 40
GOOGLE = "AIza" + "C" * 35
SLACK = "xoxb-123456789012-abcdefghijkl"
STRIPE = "sk_live_" + "d" * 24
JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq...\n-----END PRIVATE KEY-----"
RSA_PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----"

REDACT = [
    ("AKIAIOSFODNN7EXAMPLE", "***"),                                    # AWS access key id
    ("ASIAJEXAMPLE1234ABCD", "***"),                                    # AWS temp key id
    (GH, "***"),                                                        # GitHub classic token
    (PAT, "***"),                                                       # GitHub fine-grained PAT
    (GOOGLE, "***"),                                                    # Google API key
    (SLACK, "***"),                                                     # Slack token
    (STRIPE, "***"),                                                    # Stripe key
    (JWT, "***"),                                                       # JWT
    (PEM, "***"),                                                       # PEM key material (whole value)
    (RSA_PEM, "***"),                                                   # RSA PEM
    # embedded credentials in a URL → partial: scheme + host survive, creds masked
    (
        "postgres://admin:s3cr3t@db.internal:5432/billing",
        "postgres://***:***@db.internal:5432/billing",
    ),
    ("mongodb://u:p@10.0.0.1:27017/app", "mongodb://***:***@10.0.0.1:27017/app"),
    # embedded token inside a larger string → masked in place, rest kept
    (
        "https://hooks.slack.com/services/T00/B01/" + SLACK,
        "https://hooks.slack.com/services/T00/B01/***",
    ),
]


@pytest.mark.parametrize("value", KEEP)
def test_benign_values_are_never_redacted(value: str) -> None:
    assert redact_secrets(value) == value


@pytest.mark.parametrize("value,expected", REDACT)
def test_secrets_are_redacted(value: str, expected: str) -> None:
    out = redact_secrets(value)
    assert out == expected
    # no fragment of the original secret survives the redaction
    if "***" == expected:
        assert value not in out


def test_only_the_credential_span_is_removed_not_the_host() -> None:
    out = redact_secrets("postgres://admin:s3cr3t@db.internal:5432/billing")
    assert "s3cr3t" not in out and "admin" not in out  # credentials gone
    assert "db.internal:5432/billing" in out            # useful structure kept


# ── integration: a secret value under an innocuous key is redacted in the TOON ──
def _toon(obj) -> str:
    src = json.dumps(obj, ensure_ascii=False).encode()
    ctx = ParseContext(
        path="a.json",
        abs_path=None,
        source=src,
        repo_root=".",
        capture_statements=True,
        statement_text_limit=8000,
    )
    return StructuredJsonParser().parse_file(ctx).statements[0].text


def test_value_shaped_secret_redacted_even_under_innocuous_key() -> None:
    # keys `dsn` / `awsKey` do NOT match layer-1's key list — layer 2 must still catch them
    toon = _toon([{"host": "h1", "dsn": "postgres://admin:s3cr3t@db/x", "awsKey": "AKIAIOSFODNN7EXAMPLE"}])
    assert "s3cr3t" not in toon and "AKIAIOSFODNN7EXAMPLE" not in toon
    assert "***:***@db/x" in toon
    assert "h1" in toon  # non-secret sibling untouched


def test_benign_hash_survives_capture() -> None:
    toon = _toon([{"commit": "d41d8cd98f00b204e9800998ecf8427e", "tag": "v1.2.3"}])
    assert "d41d8cd98f00b204e9800998ecf8427e" in toon  # not mistaken for a secret
    assert "v1.2.3" in toon
