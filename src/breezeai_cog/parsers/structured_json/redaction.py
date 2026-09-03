"""Value-level secret redaction for structured-JSON capture (layer 2).

Layer 1 (in :class:`~breezeai_cog.parsers.structured_json.parser.StructuredJsonParser`)
redacts a leaf when its *key name* looks secret (``password`` / ``token`` / …). This
module is layer 2: it redacts a string *value* when the value itself matches a
**structurally unambiguous** secret format — a PEM private-key block, a provider token
with a distinctive prefix and fixed shape, a JWT's three-segment structure, or
credentials embedded in a ``scheme://user:pass@`` URL.

Design rule — **precision over recall: no false positives.** Every pattern is anchored
and length-fixed so that a benign value cannot match: a hex/MD5/SHA hash, a git SHA, a
UUID, a base64 asset, a plain URL (even with a ``host:port``), a version string, or a
lone base64-encoded-JSON segment all pass through untouched. There is deliberately **no**
entropy / "looks random" detection here — randomness is not a secret *format*, and
flagging it would redact legitimate data (the one error this module refuses to make). A
real secret hiding under an innocuous key with no recognizable value shape is left to
layer 1's key-name check rather than guessed at here.

Two redaction styles:

* **whole-value** — a PEM block is key material end to end, so the entire value → ``***``.
* **in-place span** — a provider token / JWT is masked where it sits (so an otherwise
  useful string keeps its non-secret parts); URL credentials collapse to
  ``scheme://***:***@`` with scheme and host preserved.
"""

from __future__ import annotations

import re

#: Whole-value guard: any value carrying PEM private-key material is dropped entirely
#: (matching only the header still leaves the base64 body, so this must win before the
#: span pass). Covers ``RSA`` / ``EC`` / ``OPENSSH`` / bare ``PRIVATE KEY`` variants.
_PEM_RE = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")

#: In-place span matches. Each branch is fixed-shape and boundary-anchored so no benign
#: string can match; the ``conn`` / ``cs`` groups drive the partial URL-credential redact.
_SECRET_RE = re.compile(
    r"""
    (?P<conn>(?P<cs>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^\s:/@]+:[^\s:/@]+@)  # scheme://user:pass@
  | \b(?:AKIA|ASIA)[0-9A-Z]{16}\b                                      # AWS access key id (20 chars)
  | \bgh[pousr]_[A-Za-z0-9]{36}\b                                      # GitHub token (classic, 40)
  | \bgithub_pat_[A-Za-z0-9_]{22,}\b                                   # GitHub fine-grained PAT
  | \bAIza[0-9A-Za-z_\-]{35}\b                                         # Google API key (39 chars)
  | \bxox[baprs]-[0-9A-Za-z-]{10,48}\b                                 # Slack token
  | \b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}\b                           # Stripe secret / restricted key
  | \beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b  # JWT: header.payload.signature
    """,
    re.VERBOSE,
)


def _mask(m: re.Match[str]) -> str:
    if m.group("conn") is not None:
        return m.group("cs") + "***:***@"  # keep scheme + host; drop only the credentials
    return "***"


def redact_secrets(value: str) -> str:
    """Redact any embedded secret in ``value``. A PEM block drops the whole value; a
    provider token / JWT is masked in place; URL credentials become ``scheme://***:***@``
    with the rest of the URL intact. Returns ``value`` unchanged when nothing matches."""
    if _PEM_RE.search(value):
        return "***"
    return _SECRET_RE.sub(_mask, value)
