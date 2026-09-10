"""Spark .read / .write / .sql detection (D7 fallback: db_method_call / dataAccessHint=spark).

Spark job code typically uses DataFrame reader/writer chains:
- ``spark.read.format("csv").load("in.csv")`` / ``spark.read.parquet(...)`` -> read
- ``df.write.format("parquet").save("out")`` / ``df.write.csv(...)`` -> write
- ``spark.sql("SELECT ...")`` -> query_statement (handled by shared text_has_query fallback)

Uses the D7 fallback: ``db_method_call`` + ``dataAccessHint="spark"`` + ``method="read"|"write"``.
If D7 is approved later, only ``_SPARK_OPS`` mapping needs to change.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Statement
from ...schemas.enums import SemanticType
from ..base import ParseContext
from ..treesitter import node_text
from ..vertx_common import enclosing_statement
from .functions import type_map
from .statements import _call_details, find_enclosing_parent_id

# Mapping of Spark terminal operations -> (semanticType, dataAccessHint, method)
# Currently using D7 fallback: ("db_method_call", "spark", <method>)
# If D7 is approved later, swap "db_method_call" -> "data_source_read" / "data_sink_write"
_SPARK_OPS: dict[str, tuple[SemanticType, str, str]] = {
    "read": ("db_method_call", "spark", "read"),
    "write": ("db_method_call", "spark", "write"),
}

_READ_METHODS = frozenset({"load", "csv", "parquet", "json", "orc", "table", "text", "textfile"})
_WRITE_METHODS = frozenset({"save", "csv", "parquet", "json", "orc", "saveastable", "insertinto", "text"})


def _spark_receiver(callee: str, operation: str) -> str | None:
    marker = f".{operation}"
    if marker not in callee:
        return None
    receiver = callee.split(marker, 1)[0]
    return receiver if receiver and "." not in receiver else None


def _type_base(type_text: str | None) -> str:
    if not type_text:
        return ""
    return type_text.split("<", 1)[0].strip().rstrip("[]").rsplit(".", 1)[-1]


def _spark_receiver_is_valid(receiver: str | None, operation: str, types: dict[str, str]) -> bool:
    if receiver is None:
        return False
    base = _type_base(types.get(receiver))
    if operation == "read":
        return base in {"SparkSession", "DataFrameReader"}
    return base in {"DataFrame", "Dataset", "DataFrameWriter"}


def _spark_types(root: Node, source: bytes) -> dict[str, str]:
    """Combine declared types with the small set of inferable Spark chain types."""
    types = type_map(root, source)
    assignments: list[tuple[str, str]] = []

    def walk(node: Node) -> None:
        if node.type in ("val_definition", "var_definition"):
            pattern = node.child_by_field_name("pattern")
            value = node.child_by_field_name("value")
            if pattern is not None and pattern.type == "identifier" and value is not None:
                assignments.append((node_text(pattern, source), node_text(value, source)))
        for child in node.named_children:
            walk(child)

    walk(root)
    for _ in range(len(assignments) + 1):
        changed = False
        for name, value in assignments:
            for operation, inferred in (("read", "DataFrame"), ("write", "DataFrameWriter")):
                receiver = _spark_receiver(value, operation)
                if _spark_receiver_is_valid(receiver, operation, types) and types.get(name) != inferred:
                    types[name] = inferred
                    changed = True
        if not changed:
            break
    return types


def _spark_op_for_call(
    callee: str, method: str, types: dict[str, str] | None = None
) -> str | None:
    """Check whether a typed Spark receiver owns this read or write chain."""
    types = types or {}
    m_lower = method.lower()
    if m_lower in _READ_METHODS:
        if _spark_receiver_is_valid(_spark_receiver(callee, "read"), "read", types):
            return "read"
    if m_lower in _WRITE_METHODS:
        if _spark_receiver_is_valid(_spark_receiver(callee, "write"), "write", types):
            return "write"
    return None


def detect_spark_calls(
    root: Node,
    ctx: ParseContext,
    record: FileRecord,
) -> None:
    """Detect Spark .read/.write calls and enrich or append statements."""
    if not ctx.capture_statements:
        return
    source = ctx.source
    if b"org.apache.spark" not in source:
        return

    seen_ids = {s.id for s in record.statements}
    types = _spark_types(root, source)
    found_any = False

    def walk(node: Node) -> None:
        nonlocal found_any
        if node.type == "call_expression":
            details = _call_details(node, source)
            if details is not None:
                callee, method, endpoint = details
                op = _spark_op_for_call(callee, method, types)
                if op is not None:
                    found_any = True
                    sem_type, hint, op_method = _SPARK_OPS[op]
                    start = node.start_point[0] + 1
                    stmt = enclosing_statement(start, record.statements)
                    if stmt is not None and stmt.semanticType is None:
                        stmt.semanticType = sem_type
                        stmt.dataAccessHint = hint
                        stmt.method = op_method
                        stmt.endpoint = endpoint
                    elif stmt is None:
                        end = node.end_point[0] + 1
                        parent_id = find_enclosing_parent_id(start, record)
                        record.statements.append(
                            Statement(
                                id=disambiguate(statement_id(ctx.path, start, node.start_point[1]), seen_ids),
                                parentId=parent_id,
                                nodeType="call_expression",
                                semanticType=sem_type,
                                text=node_text(node, source),
                                dataAccessHint=hint,
                                method=op_method,
                                endpoint=endpoint,
                                startLine=start,
                                endLine=end,
                                path=ctx.path,
                            )
                        )

        for c in node.named_children:
            walk(c)

    walk(root)
    if found_any and not record.framework:
        record.framework = "spark"
