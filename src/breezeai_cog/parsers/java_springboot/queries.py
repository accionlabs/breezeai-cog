"""Spring Data ``@Query`` detection → ``query_statement``. The JPQL/native SQL lives in
the method's ``@Query`` annotation (on a repository interface method with no body), so it
never appears as an in-body statement — we emit one at the ``@Query`` annotation, parented
to the method (Data-access view)."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import FileRecord
from ..emits import Emit
from ..java.functions import annotation_nodes, method_nodes


def detect_spring_queries(record: FileRecord, root: Node, source: bytes) -> list[Emit]:
    if not any(d.name == "Query" for fn in record.functions for d in fn.decorators):
        return []
    nodes = method_nodes(root, source)
    out: list[Emit] = []
    for fn in record.functions:
        i = next((i for i, d in enumerate(fn.decorators) if d.name == "Query"), None)
        method = nodes.get((fn.name, fn.startLine))
        if i is None or method is None:
            continue
        q = fn.decorators[i]
        anns = annotation_nodes(method)
        sql = next((a for a in q.args if a and not a.strip().lower().startswith("nativequery")), "")
        out.append(Emit(
            anns[i] if len(anns) == len(fn.decorators) else method,
            {"semanticType": "query_statement"}, parent=fn.id, text=sql or "@Query",
        ))
    return out
