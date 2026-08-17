"""StructuredJsonParser — a client-agnostic parser for *data* / *metadata* JSON (as
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

1. **Client-agnostic.** It only ever recurses over keys and values. It never interprets a
   field's *meaning* — no "this field is the identity", no per-record graph nodes. Anything
   that assigns domain meaning (naming a record from an id field, turning a filter into a
   ``query_statement``) is a client concern → a subclass at a higher ``priority``.
2. **Reliability (extend-capture skill).** Secret-named keys are redacted, string leaves are
   length-capped, and the leaf count is bounded — all applied *inside* the TOON encoder.

Selection (registry ``priority``):
* ``ConfigParser``            priority 0 — named/build configs (package.json, tsconfig, …) + flat generic JSON
* ``StructuredJsonParser``    priority 3 — JSON with record structure (array-of-objects) → full TOON capture
* ``<Client>MetadataParser``  priority 5 — client-specific refinement (subclass), if installed

``claims`` gates on record structure (a non-empty array-of-objects, at the root or under a
top-level key). This cleanly excludes ``package.json`` (dict-of-strings) / ``tsconfig.json``
(dict) / flat ``{"prefix": …}`` configs, which stay with ``ConfigParser`` untouched.
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
    #: Beats ConfigParser (0); leaves room for a client-specific subclass (priority 5).
    priority = 3
    #: `framework` value carried on the statement to name the serialization (§4.1 of the
    #: target spec: technology → `framework`). Keeps `semanticType` format-independent.
    serialization = "toon"

    #: Case-insensitive substrings; any leaf key containing one has its value redacted to
    #: ``"***"``. Applied to *keys only*, never inferred from values. Deliberately
    #: stack-neutral (no ``jdbc``/``wallet``/``connectionstring`` — those hint an
    #: architecture); a subclass may extend it. Over-redaction beats leaking a secret.
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
    #: Bound so a pathological file can't blow up the node's metadata / the embedding text.
    #: ``MAX_LEAVES`` caps how many leaves the TOON walk captures; the per-value length cap
    #: is supplied per-file via ``ParseContext.metadata_value_limit`` (Settings /
    #: ``BREEZEAI_COG_METADATA_VALUE_LIMIT``).
    MAX_LEAVES = 5000

    #: JSON files ConfigParser extracts *richly* by name — declined here even when they
    #: carry a record array (``package.json``'s ``contributors``/``funding``,
    #: ``tsconfig``'s ``references``), so a stray array-of-objects never diverts them from
    #: that rich extraction. Mirrors the JSON name-cases in
    #: ``parsers/config/extractors._dispatch``; keep the two in sync.
    CONFIG_JSON_NAMES: frozenset[str] = frozenset(
        {"package.json", "tsconfig.json", "jsconfig.json"}
    )

    # ── selection ────────────────────────────────────────────────────────────
    def claims(self, path: str, source: bytes) -> bool:
        if Path(path).name in self.CONFIG_JSON_NAMES:
            return False  # ConfigParser owns these — do not steal its rich extraction
        return self._has_record_structure(self._parse(source))

    # ── parse ────────────────────────────────────────────────────────────────
    def parse_file(self, ctx: ParseContext) -> FileRecord:
        text = ctx.source.decode("utf-8", "replace")
        data = self._parse(ctx.source)
        loc = count_loc(text)

        # Full TOON serialization of the whole document (secret-redacted, value-capped,
        # leaf-bounded — all inside the encoder). Per-value cap from ctx. The content lives
        # only on the statement below; metadata carries a structural summary, not the TOON.
        toon = encode(
            data,
            is_secret=self._is_secret_key,
            value_limit=ctx.metadata_value_limit,
            max_leaves=self.MAX_LEAVES,
        )

        meta: dict[str, Any] = {
            "kind": "structured-json",
            "category": "json",
            "format": self.serialization,
            "topLevelKeys": list(data.keys()) if isinstance(data, dict) else [],
            "recordCount": self._record_count(data),
            "leafCount": toon.leaf_count,
        }
        if toon.truncated:
            meta["truncated"] = True  # honest signal that MAX_LEAVES capped the walk

        # The whole document is captured ONLY as this statement (TOON in `text`) — a semantic
        # capture, so gated behind --capture-statements (extend-capture skill §9). The text is
        # not embedded; it is the lexical/label retrieval handle (semanticType=structured_data).
        statements: list[Statement] = []
        if ctx.capture_statements:
            statements.append(
                self._document_statement(ctx.path, toon.text, loc, ctx.statement_text_limit)
            )

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
    def _document_statement(
        self, path: str, toon_text: str, loc: int, text_limit: int
    ) -> Statement:
        """One statement standing for the whole file, TOON in ``text``. ``nodeType`` is the
        spec's ``synthetic`` sentinel (no backing tree-sitter node); ``framework`` carries
        the serialization so ``semanticType`` stays format-independent. ``endpoint`` is left
        null — it is reserved for a resolved address/target (route/URL/event address), which
        a data document has none of; the document's identity is the owning File itself
        (``parentId`` → HAS_STATEMENT, plus ``path``). Text is clipped to the statement cap."""
        clipped = toon_text
        if text_limit > 0 and len(clipped) > text_limit:
            clipped = clipped[:text_limit] + "…"
        return Statement(
            id=statement_id(path, 1, 0),
            parentId=file_id(path),
            nodeType="synthetic",
            semanticType="structured_data",
            framework=self.serialization,
            text=clipped,
            startLine=1,
            endLine=max(loc, 1),
            path=path,
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

    def _has_record_structure(self, data: Any) -> bool:
        """True if ``data`` carries a collection of records: it *is* a non-empty
        array-of-objects, or it is a dict with a top-level key mapping to one. This is the
        client-agnostic 'worth capturing in full' signal — it excludes flat configs and
        dict-of-strings files, which ConfigParser keeps."""
        if self._is_record_array(data):
            return True
        if isinstance(data, dict):
            return any(self._is_record_array(v) for v in data.values())
        return False

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
