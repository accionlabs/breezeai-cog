"""The git token a request carries.

The Breeze backend sends a repository's token encrypted: ``gitToken`` is the base64 of
``iv (12 bytes) | tag (16 bytes) | ciphertext`` and ``gitTokenAad`` is the associated data
that goes with it. The ciphertext is AES-256-GCM, with the key in
``CREDENTIALS_ENCRYPTION_KEY`` (base64 of 32 bytes), over the JSON ``{"token": "..."}``.
A request without ``gitTokenAad`` carries a raw token in ``gitToken``, as it always did.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import Settings
from .errors import ApiError

_IV_BYTES = 12
_TAG_BYTES = 16
_KEY_BYTES = 32

KEY_MISSING_MESSAGE = (
    "The git token is encrypted but no encryption key is configured on this service"
)
DECRYPT_FAILED_MESSAGE = "The git token could not be decrypted"


def request_git_token(body: dict[str, Any], settings: Settings) -> str | None:
    """The usable token for a request: decrypted when ``gitTokenAad`` is present, the raw
    ``gitToken`` otherwise, ``None`` when there is none. Never logs or echoes the value."""
    token = body.get("gitToken") or None
    aad = body.get("gitTokenAad")
    if token is None or not aad:
        return token
    return open_git_token(str(token), str(aad), settings)


def open_git_token(token: str, aad: str, settings: Settings) -> str:
    """Decrypt ``token`` with the shared key. Every failure answers the same 400, so a
    caller learns nothing about why."""
    key = _key(settings)
    try:
        raw = base64.b64decode(token, validate=True)
        if len(raw) <= _IV_BYTES + _TAG_BYTES:
            raise ValueError("too short")
        iv = raw[:_IV_BYTES]
        tag = raw[_IV_BYTES : _IV_BYTES + _TAG_BYTES]
        ciphertext = raw[_IV_BYTES + _TAG_BYTES :]
        # AESGCM expects the tag at the end of the ciphertext.
        payload = AESGCM(key).decrypt(iv, ciphertext + tag, aad.encode("utf-8"))
        value = json.loads(payload)["token"]
        if not isinstance(value, str) or not value:
            raise ValueError("no token")
        return value
    except (InvalidTag, binascii.Error, ValueError, KeyError, TypeError):
        raise ApiError(DECRYPT_FAILED_MESSAGE, 400) from None


def _key(settings: Settings) -> bytes:
    secret = settings.credentials_encryption_key
    if secret is None:
        raise ApiError(KEY_MISSING_MESSAGE, 503)
    try:
        key = base64.b64decode(secret.get_secret_value(), validate=True)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != _KEY_BYTES:
        raise ApiError(KEY_MISSING_MESSAGE, 503)
    return key
