"""Spring Data ``@Query`` detection → ``query_statement``. The JPQL/native SQL lives in
the method's ``@Query`` annotation (on a repository interface method with no body), so it
never appears as an in-body statement — we emit one from the captured decorator, parented
to the method (Data-access view)."""

from __future__ import annotations

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Statement


def detect_spring_queries(record: FileRecord) -> list[Statement]:
    seen = {s.id for s in record.statements}
    out: list[Statement] = []
    for fn in record.functions:
        q = next((d for d in fn.decorators if d.name == "Query"), None)
        if q is None:
            continue
        sql = next((a for a in q.args if a and not a.strip().lower().startswith("nativequery")), "")
        out.append(Statement(
            id=disambiguate(statement_id(fn.path, fn.startLine, 0), seen),
            parentId=fn.id,
            nodeType="synthetic",
            semanticType="query_statement",
            text=sql or "@Query",
            startLine=fn.startLine,
            endLine=fn.startLine,
            path=fn.path,
        ))
    return out
