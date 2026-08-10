"""StructuredJsonParser — a client-agnostic parser for *data* / *metadata* JSON (as
opposed to the build/config JSON that :class:`~breezeai_cog.parsers.config.parser.ConfigParser`
handles). Where ``ConfigParser`` reduces a generic ``.json`` file to
``topLevelKeys: ["values"]`` and discards the content, this parser recurses the whole
document and captures **every key and value** as a flat ``{dotted.path: value}`` map on
``FileRecord.metadata`` — which the backend folds into the File node's embedding text, so
the content becomes semantically searchable in the code graph.

Design constraints (both must hold):

1. **Client-agnostic.** It only ever recurses over keys and values. It never interprets a
   field's *meaning* — no "this field is the identity", no "this array element is a record",
   no per-element graph nodes. Anything that assigns domain meaning (naming a record from an
   id field, turning a filter into a ``query_statement``, wiring step chains into ``calls[]``)
   is a client concern and belongs in a subclass at a higher ``priority``.
2. **Usable in the code graph by an agent.** Content lands on ``File.metadata`` (an embedded
   node property) rather than on ``Function`` nodes. Mapping data records onto ``Function``
   nodes was rejected: it has no precedent in cog (config emits zero functions), it inflates
   ``projectMetaData.totalFunctions`` (the Gap-8 defect), it pollutes the ``:Function`` label
   for call-graph/complexity queries, and it multiplies embedding cost. Per-record granularity
   is a client-semantic decision → the subclass opts into it.

Selection (registry ``priority``):
* ``ConfigParser``            priority 0 — named/build configs (package.json, tsconfig, …) + flat generic JSON
* ``StructuredJsonParser``    priority 3 — JSON with record structure (array-of-objects) → full key/value capture
* ``<Client>MetadataParser``  priority 5 — client-specific refinement (subclass), if installed

``claims`` gates on record structure (a non-empty array-of-objects, at the root or under a
top-level key). This cleanly excludes ``package.json`` (dict-of-strings) / ``tsconfig.json``
(dict) / flat ``{"prefix": …}`` configs, which stay with ``ConfigParser`` untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord
from ...utils import count_loc
from ..base import BaseParser, ParseContext


class StructuredJsonParser(BaseParser):
    name = "structured-json"
    schema_version = SCHEMA_VERSION
    extensions = (".json",)
    #: Beats ConfigParser (0); leaves room for a client-specific subclass (priority 5).
    priority = 3

    #: Case-insensitive substrings; any leaf key containing one has its value redacted to
    #: ``"***"``. Applied to *keys only*, never inferred from values. Deliberately
    #: stack-neutral (no ``jdbc``/``wallet``/``connectionstring`` — those hint an
    #: architecture); a subclass may extend it. Over-redaction beats leaking a secret.
    CREDENTIAL_TOKENS: frozenset[str] = frozenset(
        {
            "password", "passwd", "secret", "token", "apikey", "api_key",
            "authorization", "credential", "private_key", "privatekey",
        }
    )
    #: Bounds so a pathological file can't blow up the node's metadata / the embedding text.
    #: ``MAX_LEAVES`` caps how many leaves are captured; the per-value length cap is supplied
    #: per-file via ``ParseContext.metadata_value_limit`` (Settings /
    #: ``BREEZEAI_COG_METADATA_VALUE_LIMIT``). ``MAX_VALUE_LEN`` is only the fallback used
    #: when ``_flatten`` / ``_leaf`` are called without an explicit limit.
    MAX_LEAVES = 5000
    MAX_VALUE_LEN = 500

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
        # full recursive key/value walk of the whole document; per-value length cap from ctx
        fields = self._flatten(data, ctx.metadata_value_limit)

        meta: dict[str, Any] = {
            "kind": "structured-json",
            "category": "json",
            "topLevelKeys": list(data.keys()) if isinstance(data, dict) else [],
            "leafCount": len(fields),
            "fields": fields,
        }
        if len(fields) >= self.MAX_LEAVES:
            meta["truncated"] = True  # honest signal that MAX_LEAVES capped the walk

        return FileRecord(
            id=file_id(ctx.path),
            path=ctx.path,
            type="config",  # data/metadata, not code — no functions/classes emitted
            language=self.name,  # distinguishes full-capture from ConfigParser's topLevelKeys
            loc=count_loc(text),
            metadata=meta,
        )

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _parse(source: bytes) -> Any:
        try:
            return json.loads(source.decode("utf-8", "replace"))
        except (ValueError, TypeError, RecursionError):
            return None

    @staticmethod
    def _is_record_array(value: Any) -> bool:
        return isinstance(value, list) and len(value) > 0 and all(isinstance(v, dict) for v in value)

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

    def _is_secret_key(self, key: str) -> bool:
        low = key.lower()
        return any(token in low for token in self.CREDENTIAL_TOKENS)

    def _flatten(self, obj: Any, max_value_len: int | None = None) -> dict[str, Any]:
        """Recursively flatten the whole document to ``{dotted.path: primitive}``. Dicts
        join with ``.``, list items with ``[i]``. ``None`` leaves are emitted as empty
        strings (so a present-but-null field is distinguishable from an absent one);
        secret-named keys are redacted; strings truncated to ``max_value_len`` (falls back
        to :attr:`MAX_VALUE_LEN`; ``<= 0`` disables truncation); bounded by
        :attr:`MAX_LEAVES`."""
        limit = self.MAX_VALUE_LEN if max_value_len is None else max_value_len
        out: dict[str, Any] = {}

        def walk(node: Any, prefix: str) -> None:
            if len(out) >= self.MAX_LEAVES:
                return
            if isinstance(node, dict):
                for k, v in node.items():
                    key = f"{prefix}.{k}" if prefix else str(k)
                    if isinstance(v, (dict, list)):
                        walk(v, key)
                    elif v is None:
                        out[key] = ""  # explicit empty: present-but-null != absent
                    elif self._is_secret_key(str(k)):
                        out[key] = "***"
                    else:
                        out[key] = self._leaf(v, limit)
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{prefix}[{i}]")
            elif node is not None:
                out[prefix] = self._leaf(node, limit)
            elif prefix:  # a null list element (root scalars are excluded by claims())
                out[prefix] = ""

        walk(obj, "")
        return out

    def _leaf(self, value: Any, max_value_len: int | None = None) -> Any:
        limit = self.MAX_VALUE_LEN if max_value_len is None else max_value_len
        if limit > 0 and isinstance(value, str) and len(value) > limit:
            return value[:limit] + "…"
        return value
