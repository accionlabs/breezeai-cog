"""CQLParser — extracts Cassandra table definitions from .cql files.

Each ``CREATE TABLE`` statement becomes a Class with ``type="table"`` and
``source="cassandra"``. Primary key columns are emitted with a ``keyType``
of ``"PARTITION_KEY"`` or ``"CLUSTERING_KEY"``; all other columns carry their
CQL data type. No tree-sitter grammar is available for CQL, so this parser
uses regex-based extraction. Conservative: only emits what can be reliably
determined.
"""

from __future__ import annotations

import re

from ...emit import class_id, disambiguate, file_id
from ...schemas import SCHEMA_VERSION, Class, FileRecord
from ...utils import count_loc
from ..base import BaseParser, ParseContext

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Strip single-line (--) and block (/* */) comments
_STRIP_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)

# CREATE TABLE [IF NOT EXISTS] [keyspace.]tablename (
_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:[\w\"]+\.)?([\w\"]+)\s*\((.+?)\)\s*(WITH\s+[^;]+)?;",
    re.IGNORECASE | re.DOTALL,
)

# CLUSTERING ORDER BY (col ASC, col2 DESC) inside a WITH clause
_CLUSTERING_ORDER = re.compile(
    r"CLUSTERING\s+ORDER\s+BY\s*\(\s*(.+?)\s*\)",
    re.IGNORECASE | re.DOTALL,
)

# PRIMARY KEY clause: PRIMARY KEY (col) or PRIMARY KEY ((pk1, pk2), ck1, ck2)
_PRIMARY_KEY = re.compile(
    r"PRIMARY\s+KEY\s*\(\s*(\(.+?\)|[\w\"]+)(?:\s*,\s*([\w\s\",]+))?\s*\)",
    re.IGNORECASE | re.DOTALL,
)

# A single column definition line (not PRIMARY KEY): colname TYPE
_COLUMN_DEF = re.compile(
    r"^\s*([\w\"]+)\s+([\w<>, ]+?)(?:\s+PRIMARY\s+KEY)?\s*$",
    re.IGNORECASE,
)


def _strip_quotes(s: str) -> str:
    return s.strip().strip('"')


def _parse_clustering_order(with_clause: str) -> list[dict]:
    """Return indexes[] from CLUSTERING ORDER BY inside a WITH clause."""
    m = _CLUSTERING_ORDER.search(with_clause)
    if not m:
        return []
    columns = []
    for part in m.group(1).split(","):
        part = part.strip()
        tokens = part.split()
        if tokens:
            col = _strip_quotes(tokens[0])
            direction = tokens[1].upper() if len(tokens) > 1 else "ASC"
            columns.append({"column": col, "direction": direction})
    return [{"type": "clustering_order", "columns": columns}] if columns else []


def _parse_table(table_name: str, body: str, with_clause: str = "") -> tuple[list[dict], list[dict]]:
    """Return (columns, indexes) for a CREATE TABLE body string."""
    # Identify PRIMARY KEY columns
    pk_cols: list[str] = []
    ck_cols: list[str] = []
    pk_match = _PRIMARY_KEY.search(body)
    if pk_match:
        pk_part = pk_match.group(1).strip()
        ck_part = pk_match.group(2) or ""
        if pk_part.startswith("("):
            # Composite partition key: ((pk1, pk2), ck1)
            inner = pk_part[1:pk_part.rfind(")")].strip()
            pk_cols = [_strip_quotes(c) for c in inner.split(",")]
        else:
            pk_cols = [_strip_quotes(pk_part)]
        if ck_part.strip():
            ck_cols = [_strip_quotes(c) for c in ck_part.split(",")]

    pk_set = set(pk_cols)
    ck_set = set(ck_cols)

    columns: list[dict] = []
    # Remove the PRIMARY KEY clause so it doesn't confuse column parsing
    body_clean = _PRIMARY_KEY.sub("", body)
    for line in body_clean.split(","):
        line = line.strip()
        if not line or line.upper().startswith("WITH"):
            continue
        m = _COLUMN_DEF.match(line)
        if m is None:
            continue
        col_name = _strip_quotes(m.group(1))
        col_type = m.group(2).strip()
        # Skip if it looks like a standalone PRIMARY KEY declaration
        if col_name.upper() in ("PRIMARY", "WITH"):
            continue
        col: dict = {"name": col_name, "dataType": col_type}
        if col_name in pk_set:
            col["keyType"] = "PARTITION_KEY"
        elif col_name in ck_set:
            col["keyType"] = "CLUSTERING_KEY"
        # Inline PRIMARY KEY shorthand (single-col PK)
        if re.search(r"PRIMARY\s+KEY", line, re.IGNORECASE) and col_name not in pk_set:
            col["keyType"] = "PARTITION_KEY"
        columns.append(col)

    indexes = _parse_clustering_order(with_clause)
    return columns, indexes


class CQLParser(BaseParser):
    """Regex-based Cassandra CQL parser. No tree-sitter grammar available."""

    name = "cql"
    extensions: tuple[str, ...] = (".cql",)
    schema_version = SCHEMA_VERSION
    statement_types: list[str] = []
    frameworks: list[str] = []

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        source = ctx.source
        path = ctx.path
        text = source.decode("utf-8", "replace")
        fid = file_id(path)
        seen_ids: set[str] = set()
        classes: list[Class] = []

        clean = _STRIP_COMMENTS.sub(" ", text)
        total_lines = text.count("\n") + 1

        for m in _CREATE_TABLE.finditer(clean):
            table_name = _strip_quotes(m.group(1))
            body = m.group(2)
            with_clause = m.group(3) or ""
            columns, indexes = _parse_table(table_name, body, with_clause)
            # Approximate line numbers from character position
            start_line = text[: m.start()].count("\n") + 1
            end_line = min(text[: m.end()].count("\n") + 1, total_lines)
            cid = disambiguate(class_id(path, table_name), seen_ids)
            classes.append(
                Class(
                    id=cid,
                    parentId=fid,
                    path=path,
                    name=table_name,
                    type="table",
                    startLine=start_line,
                    endLine=end_line,
                    source="cassandra",
                    columns=columns,
                    indexes=indexes,
                )
            )

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="cql",
            loc=count_loc(text),
            classes=classes,
        )
