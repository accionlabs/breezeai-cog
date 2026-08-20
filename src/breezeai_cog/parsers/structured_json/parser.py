"""StructuredJsonParser — a domain-agnostic parser for *data* / *metadata* JSON (as
opposed to the build/config JSON that :class:`~breezeai_cog.parsers.config.parser.ConfigParser`
handles). Where ``ConfigParser`` reduces a generic ``.json`` file to
``topLevelKeys: ["values"]`` and discards the content, this parser recurses the whole
document and captures **every key and value**, serialized as **TOON** (see :mod:`.toon`) —
a compact, uniform-array-aware form that is far denser than a per-leaf dotted map.

The whole document is captured as a single ``structured_data`` **statement** (``text`` = the
TOON), emitted only under ``--capture-statements``. Statements are not embedded, but they are
exactly/lexically filterable by label (``Get_Code_Nodes_By_Label`` label=Statement,
``semanticType=structured_data``), so an agent that already has (or greps for) the file can
read the whole document as one node. ``nodeType=synthetic`` (JSON is not tree-sitter parsed →
no backing AST node); the serialization is carried on ``framework=toon``.

``FileRecord.metadata`` carries only a **structural summary** (``topLevelKeys``,
``recordCount``, ``leafCount``, ``format``) — *not* the content. Note the trade-off: because
the TOON is on the (unembedded) statement and not in ``File.metadata``, the content is **not**
reachable by semantic ``Code_Graph_Search`` — only lexically by label, or once the File is
otherwise found. Concept-based discovery of JSON values is forgone by this design.

Design constraints (both must hold):

1. **Domain-agnostic.** It only ever recurses over keys and values. It never interprets a
   field's *meaning* — no "this field is the identity", no per-record graph nodes.
2. **Reliability.** Secret-named keys and secret-shaped string values are redacted *inside*
   the TOON encoder — the only transform. Nothing is truncated or dropped; an oversized
   document is split into ``#partNofN`` statements at emit (``emit.split``).

Selection (registry ``priority``):
* ``ConfigParser``            priority 0 — the ``CONFIG_JSON_NAMES`` rich extractors + empty/scalar JSON
* ``StructuredJsonParser``    priority 3 — every other non-empty JSON → full TOON capture

``claims`` captures any non-empty JSON container (dict or list) whose filename is not in
``CONFIG_JSON_NAMES`` — shape-independent, so a lookup map, a nested tree, and a
heterogeneous config are all captured, not just a record array. The named rich configs
(``package.json`` / ``tsconfig`` / ``mod.json`` …) and empty/scalar roots stay with
``ConfigParser``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...emit import file_id, statement_id
from ...schemas import SCHEMA_VERSION, FileRecord, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from .toon import encode


class StructuredJsonParser(BaseParser):
    name = "structured-json"
    schema_version = SCHEMA_VERSION
    extensions = (".json",)
    #: Beats ConfigParser (0), so it wins for any non-empty JSON it claims.
    priority = 3
    #: `framework` value carried on the statement to name the serialization (§4.1 of the
    #: target spec: technology → `framework`). Keeps `semanticType` format-independent.
    serialization = "toon"

    #: Case-insensitive substrings; any leaf key containing one has its value redacted to
    #: ``"***"``. Applied to *keys only*, never inferred from values. Deliberately
    #: stack-neutral (no ``jdbc``/``wallet``/``connectionstring`` — those hint an
    #: architecture). Over-redaction beats leaking a secret.
    CREDENTIAL_TOKENS: frozenset[str] = frozenset(
        {
            "password",
            "passwd",
            "secret",
            "token",
            "apikey",
            "api_key",
            "authorization",
            "credential",
            "private_key",
            "privatekey",
        }
    )
    #: JSON files ConfigParser extracts *richly* by name — declined here so their rich
    #: extraction is never replaced by full capture (``package.json``'s
    #: ``contributors``/``funding``, ``tsconfig``'s ``references``, a ``mod.json`` map).
    #: Mirrors the JSON name-cases in ``parsers/config/extractors._dispatch``; keep the
    #: two in sync. Every OTHER non-empty JSON is captured in full.
    CONFIG_JSON_NAMES: frozenset[str] = frozenset(
        {"package.json", "tsconfig.json", "jsconfig.json", "mod.json"}
    )

    # ── selection ────────────────────────────────────────────────────────────
    def claims(self, path: str, source: bytes) -> bool:
        if Path(path).name in self.CONFIG_JSON_NAMES:
            return False  # ConfigParser owns these — do not steal its rich extraction
        return self._is_capturable(self._parse(source))

    @staticmethod
    def _is_capturable(data: Any) -> bool:
        """Capture any non-empty JSON container (dict or list) in full — the domain-agnostic
        'worth capturing' signal. Shape is irrelevant: a record array, a
        lookup map, a nested tree, or a heterogeneous config all qualify. Only empty
        containers and scalar/None roots (malformed JSON) have nothing to capture and are
        left to ConfigParser."""
        return isinstance(data, (dict, list)) and len(data) > 0

    # ── parse ────────────────────────────────────────────────────────────────
    def parse_file(self, ctx: ParseContext) -> FileRecord:
        text = ctx.source.decode("utf-8", "replace")
        data = self._parse(ctx.source)
        loc = count_loc(text)

        # Full TOON serialization of the whole document (secret-redacted inside the encoder).
        # Nothing is truncated: the whole document is kept and an oversized TOON is split into
        # `#partNofN` statements downstream (emit.split). The content lives only on the
        # statement below; metadata carries a structural summary.
        toon = encode(data, is_secret=self._is_secret_key)

        meta: dict[str, Any] = {
            "kind": "structured-json",
            "category": "json",
            "format": self.serialization,
            "topLevelKeys": list(data.keys()) if isinstance(data, dict) else [],
            "recordCount": self._record_count(data),
            "leafCount": toon.leaf_count,
        }

        # The whole document is captured ONLY as this statement (TOON in `text`) — a semantic
        # capture, so gated behind --capture-statements. The text is
        # not embedded; it is the lexical/label retrieval handle (semanticType=structured_data).
        statements: list[Statement] = []
        if ctx.capture_statements:
            statements.append(self._document_statement(ctx.path, toon.text, loc))

        return FileRecord(
            id=file_id(ctx.path),
            path=ctx.path,
            type="config",  # data/metadata, not code — no functions/classes emitted
            language=self.name,  # distinguishes full-capture from ConfigParser's topLevelKeys
            loc=loc,
            metadata=meta,
            statements=statements,
        )

    # ── helpers ──────────────────────────────────────────────────────────────
    def _document_statement(self, path: str, toon_text: str, loc: int) -> Statement:
        """One statement standing for the whole file, TOON in ``text``. ``nodeType`` is the
        spec's ``synthetic`` sentinel (no backing tree-sitter node); ``framework`` carries
        the serialization so ``semanticType`` stays format-independent. ``endpoint`` is left
        null — it is reserved for a resolved address/target (route/URL/event address), which
        a data document has none of; the document's identity is the owning File itself
        (``parentId`` → HAS_STATEMENT, plus ``path``). The full TOON is kept here; sizing to
        the statement cap (splitting into ``#partNofN`` records) happens once at emit
        (``emit.split.split_oversized_statements``), so a large document is never dropped."""
        return Statement(
            id=statement_id(path, 1, 0),
            parentId=file_id(path),
            nodeType="synthetic",
            semanticType="structured_data",
            framework=self.serialization,
            text=toon_text,
            startLine=1,
            endLine=max(loc, 1),
            path=path,
            name=Path(path).name,
        )

    @staticmethod
    def _parse(source: bytes) -> Any:
        try:
            return json.loads(source.decode("utf-8", "replace"))
        except (ValueError, TypeError, RecursionError):
            return None

    @staticmethod
    def _is_record_array(value: Any) -> bool:
        return (
            isinstance(value, list) and len(value) > 0 and all(isinstance(v, dict) for v in value)
        )

    def _record_count(self, data: Any) -> int:
        """Element count of the primary record array (the root array, or the first
        top-level key mapping to one) — the honest ``[N]`` the TOON header declares."""
        if self._is_record_array(data):
            return len(data)
        if isinstance(data, dict):
            for v in data.values():
                if self._is_record_array(v):
                    return len(v)
        return 0

    def _is_secret_key(self, key: str) -> bool:
        low = key.lower()
        return any(token in low for token in self.CREDENTIAL_TOKENS)
