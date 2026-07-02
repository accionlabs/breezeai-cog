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

# SQL*Plus / client directives (not SQL) that Oracle scripts interleave between
# statements — e.g. ``rem`` (remark), ``Prompt``, ``SET FEEDBACK 1``, ``spool``.
# sqlglot has no SQL*Plus support: these either hard-error or, worse, mis-parse into
# bogus nodes (``REMARK x`` becomes an alias), and a leading directive corrupts the
# statement-boundary detection of the following CREATE. So we drop whole directive
# lines before parsing (see _strip_sqlplus). ``start`` is deliberately excluded — it
# collides with the ``START WITH`` clause of CREATE SEQUENCE; ``@``/``@@`` already
# cover the SQL*Plus "run a script" case.
_SQLPLUS_DIRECTIVE = re.compile(
    r"^\s*(rem(?:ark)?|prompt|pro|set|spool|define|def|undefine|undef|whenever|column|col|"
    r"ttitle|btitle|repheader|repfooter|break|compute|accept|acc|pause|connect|conn|"
    r"disconnect|disc|variable|var|show|clear|cl|store|save|host|describe|desc|"
    r"exec(?:ute)?|print|timing|@@?)(\s|$)",
    re.IGNORECASE,
)

# Opening -> closing delimiter for Oracle alternative quoting: q'[...]', q'{...}', etc.
_QQUOTE_CLOSERS = {"[": "]", "{": "}", "(": ")", "<": ">"}


def _scan_line(line: str, state: list[Any]) -> list[Any]:
    """Advance the string/comment scanner across one physical line and return the
    resulting ``[in_str, in_block, q_end]`` state. Tracks normal ``'…'`` strings (with
    ``''`` escapes), Oracle ``q'X…X'`` alternative quoting, and ``/* … */`` block
    comments — all of which can span lines — while treating ``--`` as a line comment.
    This is what lets a multi-line ``COMMENT … IS '…'`` survive directive stripping and
    prevents a stray apostrophe in a comment from flipping string state."""
    in_str, in_block, q_end = state
    i, n = 0, len(line)
    while i < n:
        if in_block:
            j = line.find("*/", i)
            if j == -1:
                break
            in_block, i = False, j + 2
        elif q_end is not None:
            j = line.find(q_end, i)
            if j == -1:
                break
            i, q_end = j + len(q_end), None
        elif in_str:
            if line[i] == "'":
                if i + 1 < n and line[i + 1] == "'":
                    i += 2  # '' -> escaped quote, stay in string
                else:
                    in_str, i = False, i + 1
            else:
                i += 1
        else:
            two = line[i : i + 2]
            if two == "--":
                break  # rest of the physical line is a comment
            if two == "/*":
                in_block, i = True, i + 2
            elif (
                line[i] in "qQ"
                and i + 1 < n
                and line[i + 1] == "'"
                and (i == 0 or not (line[i - 1].isalnum() or line[i - 1] == "_"))
            ):
                delim = line[i + 2] if i + 2 < n else "'"
                q_end, i = _QQUOTE_CLOSERS.get(delim, delim) + "'", i + 3
            elif line[i] == "'":
                in_str, i = True, i + 1
            else:
                i += 1
    return [in_str, in_block, q_end]


def _strip_sqlplus(text: str) -> str:
    """Remove SQL*Plus client directive lines so the remainder is pure SQL. A directive
    line is dropped only when the scanner is not currently inside a string or block
    comment, so continuation lines of a multi-line literal are preserved."""
    out: list[str] = []
    state = [False, False, None]  # [in_str, in_block, q_end]
    for line in text.splitlines():
        inside = state[0] or state[1] or state[2] is not None
        if not inside and (line.strip() == "/" or _SQLPLUS_DIRECTIVE.match(line)):
            continue  # SQL*Plus directive or run-buffer terminator — not SQL, and not
            # scanned for state (a directive cannot legally open a SQL string).
        state = _scan_line(line, state)
        out.append(line)
    return "\n".join(out)


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


def _extract_constraint(node: exp.Expression, table_name: str, cname: str | None) -> dict[str, Any] | None:
    """Build a constraint record from a table-level definition (inside CREATE TABLE) or
    an ``ALTER TABLE ADD CONSTRAINT`` action — both wrap the same PK/FK/UNIQUE nodes."""
    fk = node if isinstance(node, exp.ForeignKey) else node.find(exp.ForeignKey)
    pk = node if isinstance(node, exp.PrimaryKey) else node.find(exp.PrimaryKey)
    unique = node if isinstance(node, exp.UniqueColumnConstraint) else node.find(exp.UniqueColumnConstraint)
    if fk is not None:
        return _foreign_key(fk, table_name, cname)
    if pk is not None:
        return {"name": cname, "tableName": table_name,
                "constraintType": "PRIMARY_KEY", "columns": [i.name for i in pk.expressions]}
    if unique is not None:
        return {"name": cname, "tableName": table_name,
                "constraintType": "UNIQUE", "columns": [i.name for i in unique.find_all(exp.Identifier)]}
    return None


def _wire_columns(table: dict[str, Any]) -> None:
    """Set per-column PK/FK flags and ``hasPrimaryKey`` from the table's full constraint
    list. Run after ALTER-added constraints are attached so those count too."""
    pk_cols = {c for con in table["constraints"] if con["constraintType"] == "PRIMARY_KEY" for c in con["columns"]}
    fk_cols = {c for con in table["constraints"] if con["constraintType"] == "FOREIGN_KEY" for c in con["columns"]}
    for c in table["columns"]:
        if c["name"] in pk_cols:
            c["isPrimaryKey"] = True
        if c["name"] in fk_cols:
            c["isForeignKey"] = True
    table["hasPrimaryKey"] = bool(pk_cols) or any(c["isPrimaryKey"] for c in table["columns"])


def _table(create: exp.Create, dialect: str) -> dict[str, Any]:
    table = create.find(exp.Table)
    name = table.name if table is not None else ""
    schema = (table.db or None) if table is not None else None
    full = f"{schema}.{name}" if schema else name

    schema_expr = create.this
    defs = schema_expr.expressions if isinstance(schema_expr, exp.Schema) else []
    columns: list[dict[str, Any]] = []
    constraints: list[dict[str, Any]] = []
    ordinal = 0

    for d in defs:
        if isinstance(d, exp.ColumnDef):
            ordinal += 1
            columns.append(_column(d, ordinal, dialect))
            continue
        cname = d.name if isinstance(d, exp.Constraint) and d.name else None
        rec = _extract_constraint(d, name, cname)
        if rec is not None:
            constraints.append(rec)

    return {
        "name": name,
        "schema": schema,
        "fullName": full,
        "tableType": "table",
        "columnCount": len(columns),
        "hasPrimaryKey": False,  # set by _wire_columns after ALTERs are applied
        "columns": columns,
        "constraints": constraints,
        "indexes": [],
    }


def _apply_alter(alter: exp.Alter, by_table: dict[str, dict[str, Any]]) -> None:
    """Attach constraints from ``ALTER TABLE … ADD CONSTRAINT`` to the owning table.
    Oracle scripts commonly declare every PK/FK/UNIQUE this way rather than inline."""
    if (alter.args.get("kind") or "").upper() != "TABLE":
        return
    table = alter.find(exp.Table)
    owner = by_table.get(table.name) if table is not None else None
    if owner is None:
        return
    for act in alter.args.get("actions") or []:
        if not isinstance(act, exp.AddConstraint):
            continue
        cons = act.find(exp.Constraint)
        cname = cons.name if cons is not None and cons.name else None
        rec = _extract_constraint(act, owner["name"], cname)
        if rec is not None:
            owner["constraints"].append(rec)


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
    text = _strip_sqlplus(text)

    tables: list[dict] = []
    views: list[dict] = []
    all_indexes: list[dict] = []
    alters: list[exp.Alter] = []
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
            elif isinstance(stmt, exp.Alter):
                alters.append(stmt)
            ok += 1
        except Exception as exc:  # pragma: no cover - defensive
            failed += 1
            if len(sample_errors) < 5:
                sample_errors.append(str(exc))

    by_table = {t["name"]: t for t in tables}

    # apply ALTER TABLE ADD CONSTRAINT (PK/FK/UNIQUE declared after the table)
    for alter in alters:
        _apply_alter(alter, by_table)

    # attach table-owned indexes
    for idx in all_indexes:
        owner = by_table.get(idx["tableName"])
        if owner is not None:
            owner["indexes"].append(idx)
            for col in owner["columns"]:
                if col["name"] in idx["columns"]:
                    col["isIndexed"] = True

    # set per-column PK/FK flags now that inline + ALTER constraints are all attached
    for table in tables:
        _wire_columns(table)

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
