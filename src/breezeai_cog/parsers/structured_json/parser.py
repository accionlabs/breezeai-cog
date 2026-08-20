"""JsonParser — the single owner of ``.json`` files.

It routes each JSON to one of two representations:

* **Rich config** — named build/config files with a dedicated extractor (``package.json``,
  ``tsconfig`` / ``jsconfig``, ``mod.json`` — see ``config.extractors.RICH_JSON_NAMES``) and
  any empty/scalar JSON with nothing to capture. These become ``type="config"``,
  ``language="config"`` records whose ``metadata`` is the extractor's structural summary
  (``topLevelKeys``, dependencies, build tool, …).
* **Full capture** — every OTHER non-empty JSON (data / metadata: lookup maps, nested trees,
  record arrays, heterogeneous configs). The whole document is serialized as **TOON** (see
  :mod:`.toon`) and carried on a single ``structured_data`` **statement** (``text`` = the
  TOON), emitted only under ``--capture-statements``. ``language="structured-json"``,
  ``metadata.format="toon"``, ``nodeType=synthetic`` (JSON is not tree-sitter parsed).

Capture is domain-agnostic (it only recurses over keys and values, never interpreting a
field's meaning) and lossless (secret redaction is the only transform; nothing is truncated —
an oversized TOON is split into ``#partNofN`` statements at emit, see ``emit.split``). The
captured content lives only on the (unembedded) statement, reachable lexically by label
(``semanticType=structured_data``), not by semantic ``Code_Graph_Search``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...emit import file_id, statement_id
from ...schemas import SCHEMA_VERSION, FileRecord, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..config.extractors import RICH_JSON_NAMES, extract_config
from .toon import encode


class JsonParser(BaseParser):
    name = "json"
    schema_version = SCHEMA_VERSION
    extensions = (".json",)  # the sole ``.json`` owner (ConfigParser no longer claims .json)
    #: `framework` value carried on the statement to name the serialization. Keeps
    #: `semanticType` format-independent.
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

    # ── parse ────────────────────────────────────────────────────────────────
    def parse_file(self, ctx: ParseContext) -> FileRecord:
        text = ctx.source.decode("utf-8", "replace")
        data = self._parse(ctx.source)
        loc = count_loc(text)
        # Named-rich configs, and empty/scalar JSON with nothing to capture, are config records
        # handled by the shared extractor; every other non-empty JSON is captured in full.
        if Path(ctx.path).name in RICH_JSON_NAMES or not self._is_capturable(data):
            return FileRecord(
                id=file_id(ctx.path),
                path=ctx.path,
                type="config",
                language="config",
                loc=loc,
                metadata=extract_config(ctx.path, text),
            )
        return self._capture(ctx, data, loc)

    @staticmethod
    def _is_capturable(data: Any) -> bool:
        """A non-empty JSON container (dict or list) is captured in full — shape-independent
        (record array, lookup map, nested tree, heterogeneous config all qualify). Empty
        containers and scalar/None roots (malformed JSON) have nothing to capture."""
        return isinstance(data, (dict, list)) and len(data) > 0

    # ── full capture ─────────────────────────────────────────────────────────
    def _capture(self, ctx: ParseContext, data: Any, loc: int) -> FileRecord:
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
        # capture, so gated behind --capture-statements. The text is not embedded; it is the
        # lexical/label retrieval handle (semanticType=structured_data).
        statements: list[Statement] = []
        if ctx.capture_statements:
            statements.append(self._document_statement(ctx.path, toon.text, loc))

        return FileRecord(
            id=file_id(ctx.path),
            path=ctx.path,
            type="config",  # data/metadata, not code — no functions/classes emitted
            language="structured-json",  # distinguishes full capture from a reduced config
            loc=loc,
            metadata=meta,
            statements=statements,
        )

    # ── helpers ──────────────────────────────────────────────────────────────
    def _document_statement(self, path: str, toon_text: str, loc: int) -> Statement:
        """One statement standing for the whole file, TOON in ``text``. ``nodeType`` is the
        spec's ``synthetic`` sentinel (no backing tree-sitter node); ``framework`` carries the
        serialization so ``semanticType`` stays format-independent. ``endpoint`` is left null —
        it is reserved for a resolved address/target (route/URL/event address), which a data
        document has none of; the document's identity is the owning File itself (``parentId`` →
        HAS_STATEMENT, plus ``path``). The full TOON is kept here; sizing to the statement cap
        (splitting into ``#partNofN`` records) happens once at emit
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
        """Element count of the primary record array (the root array, or the first top-level
        key mapping to one) — the honest ``[N]`` the TOON header declares."""
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
