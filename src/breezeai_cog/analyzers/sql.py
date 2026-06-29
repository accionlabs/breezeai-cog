"""SQL DDL analyzer — port of ``sql/extract-ddl.js`` (which uses node-sql-parser),
built on ``sqlglot``. ``parse_ddl(text, filePath)`` detects the dialect and extracts
tables (columns, constraints, PK/FK/unique/nullable), views, and indexes into the same
dialect-agnostic record shapes the JS produces, plus ``parseStats``.

Coverage note: tables/views/indexes are extracted; Oracle PL/SQL objects (procedures,
packages, triggers, sequences) are returned as empty lists for now — the JS Oracle path
is a hand-rolled parser; replicating it fully is tracked as follow-up."""

from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp

# Our dialect label -> sqlglot read dialect.
_SQLGLOT = {
    "oracle": "oracle",
    "postgresql": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "transactsql": "tsql",
    "sqlite": "sqlite",
}

_FILENAME_HINTS = [
    ("oracle", ("ora", "oracle")),
    ("postgresql", ("pg", "postgres", "postgresql")),
    ("transactsql", ("mssql", "tsql", "sqlserver")),
    ("mariadb", ("mariadb",)),
    ("mysql", ("mysql",)),
    ("sqlite", ("sqlite",)),
]

_CONTENT_SIGNATURES = {
    "oracle": r"varchar2|number\s*\(|sysdate|pls_integer",
    "postgresql": r"\bserial\b|bytea|::|\$\$",
    "mysql": r"`|auto_increment|engine\s*=|unsigned",
    "transactsql": r"\[\w+\]\.\[\w+\]|nvarchar\(max\)|identity\s*\(",
    "sqlite": r"autoincrement|without\s+rowid|pragma",
}


def detect_dialect(text: str, filepath: str | None) -> str:
    name = (filepath or "").lower()
    for dialect, hints in _FILENAME_HINTS:
        if any(h in name for h in hints):
            return dialect
    scores = {d: (1 if re.search(p, text, re.I) else 0) for d, p in _CONTENT_SIGNATURES.items()}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "postgresql"


def _type_sql(node: exp.Expression | None, dialect: str) -> str:
    if node is None:
        return ""
    try:
        return node.sql(dialect=_SQLGLOT.get(dialect, "postgres")).upper()
    except Exception:
        return node.sql().upper()


def _column(col: exp.ColumnDef, ordinal: int, dialect: str) -> dict[str, Any]:
    constraints = col.args.get("constraints") or []
    kinds = [c.kind for c in constraints if isinstance(c, exp.ColumnConstraint)]
    nullable = not any(isinstance(k, exp.NotNullColumnConstraint) for k in kinds)
    is_pk = any(isinstance(k, exp.PrimaryKeyColumnConstraint) for k in kinds)
    is_unique = any(isinstance(k, exp.UniqueColumnConstraint) for k in kinds)
    column: dict[str, Any] = {
        "name": col.name,
        "dataType": _type_sql(col.args.get("kind"), dialect),
        "nullable": nullable,
        "isPrimaryKey": is_pk,
        "isUnique": is_unique,
        "isForeignKey": False,
        "isIndexed": False,
        "ordinalPosition": ordinal,
    }
    for k in kinds:
        if isinstance(k, exp.DefaultColumnConstraint):
            column["defaultValue"] = k.this.sql() if k.this is not None else None
    return column


def _foreign_key(fk: exp.ForeignKey, table_name: str, cname: str | None = None) -> dict[str, Any]:
    cols = [i.name for i in fk.expressions]
    ref = fk.args.get("reference")
    ref_table = ref_schema = None
    ref_cols: list[str] = []
    if isinstance(ref, exp.Reference):
        schema = ref.this
        tbl = schema.find(exp.Table) if schema is not None else None
        if tbl is not None:
            ref_table = tbl.name
            ref_schema = tbl.db or None
        if isinstance(schema, exp.Schema):
            ref_cols = [e.name for e in schema.expressions if isinstance(e, exp.Identifier)]
    out: dict[str, Any] = {
        "name": cname,
        "tableName": table_name,
        "constraintType": "FOREIGN_KEY",
        "columns": cols,
        "refTableName": ref_table,
        "refColumns": ref_cols,
    }
    if ref_schema:
        out["refTableSchema"] = ref_schema
    return out


def _table(create: exp.Create, dialect: str) -> dict[str, Any]:
    table = create.find(exp.Table)
    name = table.name if table is not None else ""
    schema = (table.db or None) if table is not None else None
    full = f"{schema}.{name}" if schema else name

    schema_expr = create.this
    defs = schema_expr.expressions if isinstance(schema_expr, exp.Schema) else []
    columns: list[dict[str, Any]] = []
    constraints: list[dict[str, Any]] = []
    pk_cols: list[str] = []
    ordinal = 0

    for d in defs:
        if isinstance(d, exp.ColumnDef):
            ordinal += 1
            columns.append(_column(d, ordinal, dialect))
            continue
        cname = d.name if isinstance(d, exp.Constraint) and d.name else None
        fk = d if isinstance(d, exp.ForeignKey) else d.find(exp.ForeignKey)
        pk = d if isinstance(d, exp.PrimaryKey) else d.find(exp.PrimaryKey)
        unique = d if isinstance(d, exp.UniqueColumnConstraint) else d.find(exp.UniqueColumnConstraint)
        if fk is not None:
            constraints.append(_foreign_key(fk, name, cname))
        elif pk is not None:
            cols = [i.name for i in pk.expressions]
            pk_cols.extend(cols)
            constraints.append({"name": cname, "tableName": name,
                                "constraintType": "PRIMARY_KEY", "columns": cols})
        elif unique is not None:
            cols = [i.name for i in unique.find_all(exp.Identifier)]
            constraints.append({"name": cname, "tableName": name,
                                "constraintType": "UNIQUE", "columns": cols})

    fk_cols = {c for con in constraints if con["constraintType"] == "FOREIGN_KEY" for c in con["columns"]}
    for c in columns:
        if c["name"] in pk_cols:
            c["isPrimaryKey"] = True
        if c["name"] in fk_cols:
            c["isForeignKey"] = True

    has_pk = bool(pk_cols) or any(c["isPrimaryKey"] for c in columns)
    return {
        "name": name,
        "schema": schema,
        "fullName": full,
        "tableType": "table",
        "columnCount": len(columns),
        "hasPrimaryKey": has_pk,
        "columns": columns,
        "constraints": constraints,
        "indexes": [],
    }


def _view(create: exp.Create, dialect: str) -> dict[str, Any]:
    table = create.find(exp.Table)
    name = table.name if table is not None else ""
    schema = (table.db or None) if table is not None else None
    full = f"{schema}.{name}" if schema else name
    query = create.expression
    definition = query.sql(dialect=_SQLGLOT.get(dialect, "postgres"))[:1000] if query is not None else None
    return {
        "name": name,
        "schema": schema,
        "fullName": full,
        "viewType": "materialized_view" if create.args.get("materialized") else "view",
        "definition": definition,
        "columns": [],
    }


def _index(create: exp.Create, dialect: str) -> dict[str, Any]:
    idx = create.this
    name = idx.name if hasattr(idx, "name") else ""
    table = create.find(exp.Table)
    table_name = table.name if table is not None else None
    table_schema = (table.db or None) if table is not None else None
    cols = [c.name for c in create.find_all(exp.Column)]
    return {
        "name": name,
        "tableName": table_name,
        "tableFullName": f"{table_schema}.{table_name}" if table_schema and table_name else table_name,
        "columns": cols,
        "isUnique": bool(create.args.get("unique")),
        "indexType": "BTREE",
        "whereClause": None,
    }


def parse_ddl(text: str, filepath: str | None = None) -> dict[str, Any]:
    dialect = detect_dialect(text, filepath)
    read = _SQLGLOT.get(dialect, "postgres")

    tables: list[dict] = []
    views: list[dict] = []
    all_indexes: list[dict] = []
    ok = failed = 0
    sample_errors: list[str] = []

    try:
        statements = sqlglot.parse(text, read=read)
    except Exception:
        statements = [s for s in _safe_parse_each(text, read, sample_errors)]

    for stmt in statements:
        if stmt is None:
            failed += 1
            continue
        try:
            if isinstance(stmt, exp.Create):
                kind = (stmt.kind or "").upper()
                if kind == "TABLE":
                    tables.append(_table(stmt, dialect))
                elif kind == "VIEW":
                    views.append(_view(stmt, dialect))
                elif kind == "INDEX":
                    all_indexes.append(_index(stmt, dialect))
            ok += 1
        except Exception as exc:  # pragma: no cover - defensive
            failed += 1
            if len(sample_errors) < 5:
                sample_errors.append(str(exc))

    # attach table-owned indexes
    by_table = {t["name"]: t for t in tables}
    for idx in all_indexes:
        owner = by_table.get(idx["tableName"])
        if owner is not None:
            owner["indexes"].append(idx)
            for col in owner["columns"]:
                if col["name"] in idx["columns"]:
                    col["isIndexed"] = True

    return {
        "dialect": dialect,
        "tables": tables,
        "views": views,
        "procedures": [],
        "allIndexes": all_indexes,
        "indexes": [i for i in all_indexes if i["tableName"] is None],
        "sequences": [],
        "parseStats": {"ok": ok, "failed": failed, "sampleErrors": sample_errors},
    }


def _safe_parse_each(text: str, read: str, sample_errors: list[str]):
    for raw in text.split(";"):
        chunk = raw.strip()
        if not chunk:
            continue
        try:
            yield sqlglot.parse_one(chunk, read=read)
        except Exception as exc:
            if len(sample_errors) < 5:
                sample_errors.append(str(exc))
            yield None
