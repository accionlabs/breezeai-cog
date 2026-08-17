"""CypherParser — extracts .cypher/.cyp files into FileRecord.

Each named Cypher query (preceded by a ``// name: <name>`` comment or a
``// @query <name>`` annotation) becomes a Function with type="cypher_query".
Node labels mentioned in MATCH/MERGE/CREATE patterns (``(n:LabelName)``) are
extracted as Class records with type="graph_node". Relationship types from
``[:RELATIONSHIP_TYPE]`` become Class records with type="graph_relationship".

No tree-sitter grammar is available for Cypher, so this parser uses regex-based
extraction. Conservative: only emits what can be reliably determined.
"""

from __future__ import annotations

import re

from ...emit import class_id, disambiguate, file_id, function_id
from ...schemas import SCHEMA_VERSION, Call, Class, FileRecord, Function, Parameter
from ...utils import count_loc
from ..base import BaseParser, ParseContext

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Named query annotation: // name: foo, // @name foo, // @query foo (case-insensitive)
_NAMED_QUERY = re.compile(
    r"//\s*(?:name|@name|@query)\s*[:\s]\s*(\S+)",
    re.IGNORECASE,
)
# SQL-style named query: -- name: foo
_NAMED_QUERY_SQL = re.compile(
    r"--\s*(?:name|@name|@query)\s*[:\s]\s*(\S+)",
    re.IGNORECASE,
)
# Node labels: (n:Label) or (:Label) or (n:Label1:Label2)
_NODE_LABEL = re.compile(r"\(\w*:([\w:]+)\)")
# Relationship types: [:REL_TYPE] or -[:REL_TYPE]->
_REL_TYPE = re.compile(r"\[:(\w+)\]")
# Parameters: $paramName
_PARAM = re.compile(r"\$(\w+)")

# Comment line (single-line Cypher comment)
_COMMENT_LINE = re.compile(r"^\s*(?://|--)", re.MULTILINE)


def _split_queries(text: str) -> list[tuple[int, str]]:
    """Split Cypher source into individual queries.

    Returns a list of (start_line_1indexed, query_text) tuples.
    Queries are split on semicolons followed by whitespace or on double blank lines.
    """
    lines = text.splitlines(keepends=True)
    queries: list[tuple[int, str]] = []
    current_lines: list[str] = []
    current_start = 1

    i = 0
    while i < len(lines):
        line = lines[i]
        current_lines.append(line)
        # Check for semicolon terminator
        stripped = line.rstrip()
        if stripped.endswith(";"):
            chunk = "".join(current_lines).rstrip()
            if chunk.strip():
                queries.append((current_start, chunk))
            i += 1
            # Skip blank lines after semicolon
            while i < len(lines) and not lines[i].strip():
                i += 1
            current_start = i + 1  # 1-indexed
            current_lines = []
            continue
        i += 1

    # Remainder after last semicolon (or if no semicolons)
    chunk = "".join(current_lines).rstrip()
    if chunk.strip():
        # Try splitting on double blank lines
        parts = re.split(r"\n\s*\n\s*\n", chunk)
        line_offset = current_start
        for part in parts:
            part = part.strip()
            if part:
                queries.append((line_offset, part))
            line_offset += part.count("\n") + 2  # approximate

    return queries


def _extract_name(query_text: str) -> str | None:
    """Extract the query name from a preceding annotation comment."""
    for pattern in (_NAMED_QUERY, _NAMED_QUERY_SQL):
        m = pattern.search(query_text)
        if m:
            return m.group(1)
    return None


def _extract_node_labels(query_text: str) -> list[str]:
    """Extract unique node labels from MATCH/MERGE/CREATE patterns."""
    labels: list[str] = []
    seen: set[str] = set()
    for m in _NODE_LABEL.finditer(query_text):
        for label in m.group(1).split(":"):
            label = label.strip()
            if label and label not in seen:
                seen.add(label)
                labels.append(label)
    return labels


def _extract_rel_types(query_text: str) -> list[str]:
    """Extract unique relationship types from pattern clauses."""
    types: list[str] = []
    seen: set[str] = set()
    for m in _REL_TYPE.finditer(query_text):
        rtype = m.group(1).strip()
        if rtype and rtype not in seen:
            seen.add(rtype)
            types.append(rtype)
    return types


def _extract_params(query_text: str) -> list[str]:
    """Extract unique parameter names ($param) from the query."""
    params: list[str] = []
    seen: set[str] = set()
    for m in _PARAM.finditer(query_text):
        p = m.group(1)
        if p not in seen:
            seen.add(p)
            params.append(p)
    return params


def _line_count(text: str) -> int:
    return text.count("\n") + 1


class CypherParser(BaseParser):
    """Regex-based Cypher query parser. No tree-sitter grammar available."""

    name = "cypher"
    extensions: tuple[str, ...] = (".cypher", ".cyp")
    schema_version = SCHEMA_VERSION
    statement_types: list[str] = []
    frameworks: list[str] = []

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        source = ctx.source
        path = ctx.path
        text = source.decode("utf-8", "replace")
        fid = file_id(path)
        seen_ids: set[str] = set()

        functions: list[Function] = []
        classes: list[Class] = []
        # Track unique labels/rels across the whole file (de-duplicate across queries)
        seen_labels: set[str] = set()
        seen_rels: set[str] = set()

        queries = _split_queries(text)
        total_lines = text.count("\n") + 1

        for q_idx, (start_line, q_text) in enumerate(queries):
            q_name = _extract_name(q_text)
            if q_name is None:
                q_name = f"query_{q_idx + 1}"
            end_line = start_line + _line_count(q_text) - 1
            end_line = min(end_line, total_lines)

            fid_fn = disambiguate(function_id(path, q_name, start_line), seen_ids)
            params_list = _extract_params(q_text)
            query_labels = _extract_node_labels(q_text)

            # Strip annotation comment lines to get clean query text
            q_source = "\n".join(
                line for line in q_text.splitlines()
                if not _COMMENT_LINE.match(line)
            ).strip()
            fn = Function(
                id=fid_fn,
                parentId=fid,
                path=path,
                name=q_name,
                type="cypher_query",
                startLine=start_line,
                endLine=end_line,
                params=[Parameter(name=p, type="Any") for p in params_list],
                calls=[Call(name=label, path=None) for label in query_labels],
                sourceCode=q_source,
            )
            functions.append(fn)

            # Node labels (de-duplicated across the file; calls[] above references per-query)
            for label in query_labels:
                if label in seen_labels:
                    continue
                seen_labels.add(label)
                cid = disambiguate(class_id(path, label), seen_ids)
                classes.append(
                    Class(
                        id=cid,
                        parentId=fid,
                        path=path,
                        name=label,
                        type="graph_node",
                        startLine=start_line,
                        endLine=end_line,
                        source="neo4j-cypher",
                    )
                )

            # Relationship types
            for rtype in _extract_rel_types(q_text):
                if rtype in seen_rels:
                    continue
                seen_rels.add(rtype)
                cid = disambiguate(class_id(path, rtype), seen_ids)
                classes.append(
                    Class(
                        id=cid,
                        parentId=fid,
                        path=path,
                        name=rtype,
                        type="graph_relationship",
                        startLine=start_line,
                        endLine=end_line,
                        source="neo4j-cypher",
                    )
                )

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="cypher",
            loc=count_loc(text),
            functions=functions,
            classes=classes,
        )
