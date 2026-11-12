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
#: The URL scheme is length-bounded (BREEZEAI-1457): unbounded, every start position in a long
#: run of scheme characters with no ``://`` rescanned the rest of the run — quadratic time.
_SECRET_RE = re.compile(
    r"""
    (?P<conn>(?P<cs>[a-zA-Z][a-zA-Z0-9+.\-]{0,31}://)[^\s:/@]+:[^\s:/@]+@)  # scheme://user:pass@
  | \b(?:AKIA|ASIA)[0-9A-Z]{16}\b                                      # AWS access key id (20 chars)
  | \bgh[pousr]_[A-Za-z0-9]{36}\b                                      # GitHub token (classic, 40)
  | \bgithub_pat_[A-Za-z0-9_]{22,}\b                                   # GitHub fine-grained PAT
  | \bAIza[0-9A-Za-z_\-]{35}\b                                         # Google API key (39 chars)
  | \bxox[baprs]-[0-9A-Za-z-]{10,48}\b                                 # Slack token
  | \b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}\b                           # Stripe secret / restricted key
  | \bsk-ant-[A-Za-z0-9_\-]{20,}                                       # Anthropic API key
  | \bsk-proj-[A-Za-z0-9_\-]{20,}                                      # OpenAI project key
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


#: A PEM private-key header. :func:`redact_text` masks the block it opens line by line.
_PEM_HEADER_RE = re.compile(r"-----BEGIN ((?:[A-Z0-9]+ )*)PRIVATE KEY-----")
#: A line of an unterminated PEM body: base64 (a short last line only after a full one), or an
#: encrypted key's ``Proc-Type`` / ``DEK-Info`` header.
_PEM_LONG_LINE = re.compile(r"[ \t]*[A-Za-z0-9+/]{16,}={0,2}[ \t]*")
_PEM_SHORT_LINE = re.compile(r"[ \t]*[A-Za-z0-9+/]+={0,2}[ \t]*")
_PEM_META_LINE = re.compile(r"[ \t]*(?:Proc-Type|DEK-Info|Comment):.*")


def _mask_lines(span: str) -> str:
    """``span`` with the content of every non-blank line replaced by ``***``; newlines are kept,
    so a statement's text lines still match the file's lines."""
    return "\n".join("***" if line.strip() else line for line in span.split("\n"))


def _pem_body_end(text: str, start: int) -> int:
    """End of an unterminated PEM body that starts right after a header at ``start``: the rest
    of the header line when it is base64, then the following base64 / PEM-header lines. Stops
    at the first other line (prose, a heading, a blank line, a code fence), so a header quoted
    in prose masks nothing after it."""
    nl = text.find("\n", start)
    line_end = len(text) if nl == -1 else nl
    rest = text[start:line_end]
    if rest.strip() and not _PEM_SHORT_LINE.fullmatch(rest):
        return start  # prose after the header: not key material
    end = line_end
    long_seen = bool(rest.strip()) and bool(_PEM_LONG_LINE.fullmatch(rest))
    while end < len(text):
        nl = text.find("\n", end + 1)
        line_end = len(text) if nl == -1 else nl
        line = text[end + 1 : line_end]
        if _PEM_LONG_LINE.fullmatch(line) or _PEM_META_LINE.fullmatch(line):
            long_seen = long_seen or not _PEM_META_LINE.fullmatch(line)
        elif not (long_seen and _PEM_SHORT_LINE.fullmatch(line)):
            break
        end = line_end
    return end


def _redact_pem(text: str) -> str:
    """Mask the body of each PEM private-key block in ``text`` (BREEZEAI-1457). The body runs to
    the matching ``-----END …PRIVATE KEY-----`` when one follows before the next ``-----BEGIN``;
    without one, only the key-shaped lines after the header (:func:`_pem_body_end`) — never the
    rest of the document."""
    out: list[str] = []
    pos = 0
    m = _PEM_HEADER_RE.search(text)
    while m is not None:
        nxt = _PEM_HEADER_RE.search(text, m.end())
        limit = nxt.start() if nxt else len(text)
        footer = f"-----END {m.group(1)}PRIVATE KEY-----"
        end = text.find(footer, m.end(), limit)
        out.append(text[pos : m.end()])
        if end != -1:
            out.append(_mask_lines(text[m.end() : end]) + footer)
            pos = end + len(footer)
        else:
            pos = _pem_body_end(text, m.end())
            out.append(_mask_lines(text[m.end() : pos]))
        m = nxt
    out.append(text[pos:])
    return "".join(out)


def redact_text(text: str) -> str:
    """Redact secrets inside a free-text document (BREEZEAI-1457, Markdown capture): each line
    of a PEM private-key body becomes ``***`` in place (header and footer kept, so the line
    count is unchanged), and the same span patterns as :func:`redact_secrets` (provider tokens,
    JWTs, URL credentials) are masked where they sit. Prose around them is kept."""
    if "PRIVATE KEY-----" in text:
        text = _redact_pem(text)
    return _SECRET_RE.sub(_mask, text)
