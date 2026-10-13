"""Spring Data ``@Query`` detection → ``query_statement``. The JPQL/native SQL lives in
the method's ``@Query`` annotation (on a repository interface method with no body), so it
never appears as an in-body statement — we emit one from the captured decorator, parented
to the method (Data-access view)."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import FileRecord
from ..emits import Emit
from .routes import method_nodes


def detect_spring_queries(record: FileRecord, root: Node, source: bytes) -> list[Emit]:
    if not any(d.name == "Query" for fn in record.functions for d in fn.decorators):
        return []
    nodes = method_nodes(root, source)
    out: list[Emit] = []
    for fn in record.functions:
        q = next((d for d in fn.decorators if d.name == "Query"), None)
        if q is None:
            continue
        sql = next((a for a in q.args if a and not a.strip().lower().startswith("nativequery")), "")
        out.append(Emit(
            nodes.get((fn.name, fn.startLine), root), {"semanticType": "query_statement"},
            parent=fn.id, node_type="synthetic", text=sql or "@Query",
            line=fn.startLine, col=0, end_line=fn.startLine,
        ))
    return out
