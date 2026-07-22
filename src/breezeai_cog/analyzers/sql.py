"""SQL DDL analyzer — port of ``sql/extract-ddl.js`` (which uses node-sql-parser),
built on ``sqlglot``. ``parse_ddl(text, filePath)`` detects the dialect and extracts
tables (columns, constraints, PK/FK/unique/nullable), views, and indexes into the same
dialect-agnostic record shapes the JS produces, plus ``parseStats``.

Dialect routing: non-Oracle dialects (postgres/mysql/mariadb/tsql/sqlite) are parsed with
sqlglot — tables, views, indexes. **Oracle** is routed to a dedicated hand-rolled parser
(``_parse_oracle_ddl``, ported from the JS ``sql/extract-ddl-oracle.js``) because sqlglot
cannot parse real Oracle dumps: physical-storage clauses on tables (``USING INDEX`` /
``STORAGE`` / ``PCTFREE`` / ``TABLESPACE`` / ``SEGMENT CREATION``) raise a ``ParseError``,
and PL/SQL program bodies degrade to a generic ``Command`` (logging "contains unsupported
syntax. Falling back to parsing as a 'Command'"). The Oracle parser extracts tables
(columns + inline/table-level constraints), views, indexes, sequences, stored programs
(procedures/functions/packages/triggers) and applies ``COMMENT ON`` + ``ALTER TABLE``
mutations in file order."""

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
    text = _strip_sqlplus(text)

    # Oracle: sqlglot can't parse real Oracle dumps — physical-storage clauses on tables
    # (USING INDEX / STORAGE / PCTFREE / TABLESPACE / SEGMENT CREATION) make it raise a
    # ParseError, and PL/SQL program bodies degrade to a generic Command. So route the
    # whole Oracle dialect through the ported hand-rolled parser (see _parse_oracle_ddl).
    if dialect == "oracle":
        return {"dialect": dialect, **_parse_oracle_ddl(text)}

    read = _SQLGLOT.get(dialect, "postgres")

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

    # Non-Oracle dialects: stored programs / sequences are not extracted by the sqlglot
    # path (unchanged from before). Oracle returns early above via _parse_oracle_ddl.
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


# ---------------------------------------------------------------------------
# Oracle PL/SQL objects (procedures / functions / packages / triggers) + sequences.
# Ported from the JS hand-rolled parser (sql/extract-ddl-oracle.js). sqlglot cannot
# model PL/SQL bodies — it degrades a CREATE OR REPLACE FUNCTION/PROCEDURE/PACKAGE/
# TRIGGER to a generic Command node — so these object kinds are extracted here from the
# raw (SQL*Plus-stripped) text with a PL/SQL-aware statement splitter + header regexes.
# ---------------------------------------------------------------------------

# Oracle identifier: a double-quoted name (case preserved, "" escapes an inner quote) or
# a bare identifier. Mirrors IDENT_RE_SRC in the JS parser.
_IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$#]*)'

_PROC_RE = re.compile(
    r"^CREATE\s+(?:OR\s+REPLACE\s+)?(?:EDITIONABLE\s+|NONEDITIONABLE\s+)?"
    r"(PROCEDURE|FUNCTION|PACKAGE(?:\s+BODY)?|TRIGGER)\s+"
    r"(?:(" + _IDENT + r")\.)?(" + _IDENT + r")\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)
_PARAM_RE = re.compile(
    r"^(" + _IDENT + r")\s+(IN\s+OUT(?:\s+NOCOPY)?|IN|OUT(?:\s+NOCOPY)?)?\s*"
    r"(.+?)(?:\s+DEFAULT\s+.+)?$",
    re.IGNORECASE | re.DOTALL,
)
_SEQ_RE = re.compile(
    r"^CREATE\s+SEQUENCE\s+(?:(" + _IDENT + r")\.)?(" + _IDENT + r")\b(.*)",
    re.IGNORECASE | re.DOTALL,
)
_PROC_HEAD_RE = re.compile(
    r"^CREATE\s+(?:OR\s+REPLACE\s+)?(?:EDITIONABLE\s+|NONEDITIONABLE\s+)?"
    r"(?:PROCEDURE|FUNCTION|PACKAGE|TRIGGER)\b",
    re.IGNORECASE,
)


def _unquote_ident(raw: str | None) -> str | None:
    """Normalize an Oracle identifier: a ``"Quoted"`` name keeps its case (with ``""``
    unescaped), a bare name is upper-cased (Oracle folds unquoted identifiers)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        return s[1:-1].replace('""', '"')
    return s.upper()


def _find_matching_paren(s: str, open_pos: int) -> int:
    """Index of the ``)`` matching the ``(`` at ``open_pos`` (or -1 if unbalanced)."""
    depth = 0
    for i in range(open_pos, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_paren_aware(body: str) -> list[str]:
    """Split ``body`` on top-level commas, respecting nested parentheses (so a param
    ``NUMBER(10,2)`` stays one item). Mirrors splitColumnDefs in the JS parser."""
    items: list[str] = []
    cur = ""
    depth = 0
    for ch in body:
        if ch == "(":
            depth += 1
            cur += ch
        elif ch == ")":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            t = cur.strip()
            if t:
                items.append(t)
            cur = ""
        else:
            cur += ch
    t = cur.strip()
    if t:
        items.append(t)
    return items


def _split_col_list(s: str) -> list[str]:
    """Split a comma-separated identifier list, normalizing each entry, tolerating
    commas inside double-quoted identifiers."""
    if not s:
        return []
    out: list[str] = []
    cur = ""
    in_quote = False
    for ch in s:
        if ch == '"':
            in_quote = not in_quote
            cur += ch
        elif ch == "," and not in_quote:
            t = cur.strip()
            if t:
                out.append(_unquote_ident(t))
            cur = ""
        else:
            cur += ch
    t = cur.strip()
    if t:
        out.append(_unquote_ident(t))
    return out


def _is_plsql_block_start(buffer: str) -> bool:
    """True if the accumulated statement looks like the head of a PL/SQL block, so its
    inner ``;`` terminators must not split the outer statement."""
    head = re.sub(r"^(?:\s|--[^\n]*\n)+", "", buffer).upper()
    if not head:
        return False
    if re.match(r"DECLARE\b", head):
        return True
    if re.match(r"BEGIN\b\s*\S", head):  # a bare `BEGIN;` is a txn marker, not a block
        return True
    return bool(
        re.match(
            r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:EDITIONABLE\s+|NONEDITIONABLE\s+)?"
            r"(?:PROCEDURE|FUNCTION|PACKAGE(?:\s+BODY)?|TRIGGER|TYPE\s+BODY)\b",
            head,
        )
    )


def _detect_plsql_wrapper(buffer: str) -> str | None:
    """Classify a PL/SQL block's wrapper so BEGIN/END balancing knows the terminating
    rule: PACKAGE / TYPE BODY add a trailing END with no BEGIN (``ends == begins + 1``);
    PROCEDURE / FUNCTION / TRIGGER and anonymous blocks are balanced (``begins == ends``)."""
    head = re.sub(r"^(?:\s|--[^\n]*\n|/\*.*?\*/)+", "", buffer, flags=re.DOTALL).upper()
    if not head:
        return None
    if re.match(r"DECLARE\b", head) or re.match(r"BEGIN\b", head):
        return "anonymous"
    m = re.match(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:EDITIONABLE\s+|NONEDITIONABLE\s+)?"
        r"(PACKAGE(?:\s+BODY)?|TYPE\s+BODY|PROCEDURE|FUNCTION|TRIGGER)\b",
        head,
    )
    if not m:
        return None
    kind = m.group(1)
    if kind.startswith("PACKAGE") or re.match(r"TYPE\s+BODY", kind):
        return "wrapper-no-begin"
    return "wrapper-with-begin"


def _count_plsql_block_keywords(buffer: str) -> tuple[int, int]:
    """Count block-pairing BEGIN/END keywords in ``buffer``, ignoring comments and
    string/q-quote literals, and excluding non-pairing END forms (``END IF`` etc.)."""
    s = re.sub(r"--[^\n]*", "", buffer)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    s = re.sub(r"[qQ]'(?:\[|<|\(|\{)(.*?)(?:\]|>|\)|\})'", "''", s, flags=re.DOTALL)
    s = re.sub(r"[qQ]'(.)(.*?)\1'", "''", s, flags=re.DOTALL)
    s = re.sub(r"'(?:''|[^'])*'", "''", s)
    begins = len(re.findall(r"\bBEGIN\b", s, re.IGNORECASE))
    ends = len(
        re.findall(
            r"\bEND\b(?!\s+(?:IF|LOOP|CASE|WHILE|RECORD|OBJECT|MAP|BLOCK)\b)",
            s,
            re.IGNORECASE,
        )
    )
    return begins, ends


def _is_plsql_block_terminated(buffer: str) -> bool:
    wrapper = _detect_plsql_wrapper(buffer)
    if not wrapper:
        return False
    begins, ends = _count_plsql_block_keywords(buffer)
    if wrapper == "wrapper-no-begin":
        return ends == begins + 1
    return begins > 0 and begins == ends


def _split_statements(ddl: str) -> list[str]:
    """Split DDL into statements on ``;`` at paren-depth 0, keeping PL/SQL block bodies
    (PROCEDURE/FUNCTION/PACKAGE/TRIGGER/anonymous) intact — their inner ``;`` are
    terminators inside the block, ended by a lone ``/`` line or balanced BEGIN/END.
    Aware of ``--``/``/* */`` comments, ``'…'`` strings (with ``''``), and Oracle
    ``q'X…X'`` quoting. Port of splitStatements in the JS Oracle parser."""
    statements: list[str] = []
    current = ""
    depth = 0
    in_string = False
    i = 0
    n = len(ddl)
    while i < n:
        ch = ddl[i]
        if not in_string and ch == "/" and i + 1 < n and ddl[i + 1] == "*":
            end = ddl.find("*/", i + 2)
            if end == -1:
                break
            current += ddl[i : end + 2]
            i = end + 2
            continue
        if not in_string and ch == "-" and i + 1 < n and ddl[i + 1] == "-":
            end = ddl.find("\n", i)
            line_end = n if end == -1 else end + 1
            current += ddl[i:line_end]
            i = line_end
            continue
        if not in_string and ch in "qQ" and i + 1 < n and ddl[i + 1] == "'":
            opener = ddl[i + 2] if i + 2 < n else None
            if opener is not None:
                search = _QQUOTE_CLOSERS.get(opener, opener) + "'"
                end = ddl.find(search, i + 3)
                if end != -1:
                    current += ddl[i : end + 2]
                    i = end + 2
                    continue
        if not in_string and ch == "'":
            in_string = True
            current += ch
            i += 1
            continue
        if in_string and ch == "'":
            current += ch
            i += 1
            if i < n and ddl[i] == "'":  # '' escaped quote
                current += ddl[i]
                i += 1
            else:
                in_string = False
            continue
        if not in_string:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == ";" and depth == 0:
                if _is_plsql_block_start(current):
                    current += ch
                    if _is_plsql_block_terminated(current):
                        t = current.strip()
                        if t:
                            statements.append(t)
                        current = ""
                    i += 1
                    continue
                t = current.strip()
                if t:
                    statements.append(t)
                current = ""
                i += 1
                continue
            if ch == "/" and depth == 0:
                line_start = ddl.rfind("\n", 0, i) + 1
                le = ddl.find("\n", i)
                line = ddl[line_start : (n if le == -1 else le)].strip()
                if line == "/":
                    t = current.strip()
                    if t:
                        statements.append(t)
                    current = ""
                    i = n if le == -1 else le + 1
                    continue
        current += ch
        i += 1
    t = current.strip()
    if t:
        statements.append(t)
    return statements


def _parse_trigger_info(rest: str) -> dict[str, Any]:
    """Extract trigger timing/event/target/level/when from the header (the text before
    the PL/SQL body), so INSERT/UPDATE/DELETE inside the body don't leak into events."""
    info: dict[str, Any] = {}
    body_start = re.search(r"\b(?:DECLARE|BEGIN)\b", rest, re.IGNORECASE)
    header = rest[: body_start.start()] if body_start else rest

    tm = re.search(r"\b(BEFORE|AFTER|INSTEAD\s+OF)\b", header, re.IGNORECASE)
    if tm:
        info["timing"] = re.sub(r"\s+", " ", tm.group(1).upper())

    events: list[str] = []
    if re.search(r"\bINSERT\b", header, re.IGNORECASE):
        events.append("INSERT")
    if re.search(r"\bUPDATE\b", header, re.IGNORECASE):
        events.append("UPDATE")
        um = re.search(r"\bUPDATE\s+OF\s+([^\n;]+?)(?:\s+(?:OR|ON)\b|$)", header, re.IGNORECASE)
        if um:
            info["updateColumns"] = _split_col_list(um.group(1))
    if re.search(r"\bDELETE\b", header, re.IGNORECASE):
        events.append("DELETE")
    if events:
        info["events"] = events

    om = re.search(r"\bON\s+(?:(" + _IDENT + r")\.)?(" + _IDENT + r")", header, re.IGNORECASE)
    if om:
        if om.group(1):
            info["targetSchema"] = _unquote_ident(om.group(1))
        info["targetTable"] = _unquote_ident(om.group(2))
    info["level"] = "ROW" if re.search(r"\bFOR\s+EACH\s+ROW\b", header, re.IGNORECASE) else "STATEMENT"

    wm = re.search(r"\bWHEN\s*\((.+?)\)\s*$", header, re.IGNORECASE | re.DOTALL)
    if wm:
        info["whenCondition"] = wm.group(1).strip()
    return info


def _parse_create_procedure(stmt: str) -> dict[str, Any] | None:
    """Parse a CREATE [OR REPLACE] PROCEDURE|FUNCTION|PACKAGE[ BODY]|TRIGGER header into
    ``{name, schema, fullName, procedureType, parameters, returnType, body[, trigger]}``."""
    m = _PROC_RE.match(stmt)
    if not m:
        return None

    proc_type = re.sub(r"\s+", "_", m.group(1).upper(), count=1)  # "PACKAGE BODY" -> PACKAGE_BODY
    schema = _unquote_ident(m.group(2)) if m.group(2) else None
    name = _unquote_ident(m.group(3))
    full = f"{schema}.{name}" if schema else name
    rest = m.group(4) or ""

    parameters: list[dict[str, Any]] = []
    paren_start = rest.find("(")
    if paren_start != -1 and proc_type not in ("PACKAGE_BODY", "PACKAGE", "TRIGGER"):
        paren_end = _find_matching_paren(rest, paren_start)
        if paren_end != -1:
            for pd in _split_paren_aware(rest[paren_start + 1 : paren_end]):
                pm = _PARAM_RE.match(pd.strip())
                if pm:
                    direction = pm.group(2)
                    if direction:
                        direction = re.sub(r"\s+", " ", direction.upper())
                        direction = re.sub(r"\s*NOCOPY$", "", direction)
                    else:
                        direction = "IN"
                    parameters.append(
                        {
                            "name": _unquote_ident(pm.group(1)),
                            "direction": direction,
                            "dataType": pm.group(3).strip().upper(),
                        }
                    )

    return_type = None
    if proc_type == "FUNCTION":
        rm = re.search(
            r"\bRETURN\s+([A-Za-z_][\w$#.]*(?:\s*\([^)]*\))?(?:%(?:ROWTYPE|TYPE))?)",
            rest,
            re.IGNORECASE,
        )
        if rm:
            return_type = rm.group(1).strip().upper()

    trigger = _parse_trigger_info(rest) if proc_type == "TRIGGER" else None

    body = stmt[:1000] + ("\n-- [truncated]" if len(stmt) > 1000 else "")
    out: dict[str, Any] = {
        "name": name,
        "schema": schema,
        "fullName": full,
        "procedureType": proc_type.lower().replace("_", " "),
        "parameters": parameters,
        "returnType": return_type,
        "body": body,
    }
    if trigger:
        out["trigger"] = trigger
    return out


def _parse_create_sequence(stmt: str) -> dict[str, Any] | None:
    """Parse ``CREATE SEQUENCE [schema.]name [options]`` into a sequence descriptor."""
    m = _SEQ_RE.match(stmt)
    if not m:
        return None
    schema = _unquote_ident(m.group(1)) if m.group(1) else None
    name = _unquote_ident(m.group(2))
    options = m.group(3) or ""

    def read_num(pat: str) -> int | None:
        mm = re.search(pat, options, re.IGNORECASE)
        return int(mm.group(1)) if mm else None

    return {
        "name": name,
        "schema": schema,
        "fullName": f"{schema}.{name}" if schema else name,
        "startWith": read_num(r"\bSTART\s+WITH\s+(-?\d+)"),
        "incrementBy": read_num(r"\bINCREMENT\s+BY\s+(-?\d+)"),
        "minValue": None if re.search(r"\bNOMINVALUE\b", options, re.IGNORECASE) else read_num(r"\bMINVALUE\s+(-?\d+)"),
        "maxValue": None if re.search(r"\bNOMAXVALUE\b", options, re.IGNORECASE) else read_num(r"\bMAXVALUE\s+(-?\d+)"),
        "cache": 0 if re.search(r"\bNOCACHE\b", options, re.IGNORECASE) else read_num(r"\bCACHE\s+(\d+)"),
        "cycle": bool(re.search(r"\bCYCLE\b", options, re.IGNORECASE)) and not re.search(r"\bNOCYCLE\b", options, re.IGNORECASE),
        "order": bool(re.search(r"\bORDER\b", options, re.IGNORECASE)) and not re.search(r"\bNOORDER\b", options, re.IGNORECASE),
    }


def _to_int(s: Any) -> int | None:
    """Leading-integer parse (mirrors JS ``parseInt``): returns the int or None."""
    if s is None:
        return None
    m = re.match(r"\s*(-?\d+)", str(s))
    return int(m.group(1)) if m else None


def _parse_data_type(type_str: str) -> dict[str, Any]:
    """Split an Oracle type spec into ``{dataType, length, precision, scale, charSemantics}``:
    NUMBER(p,s) → precision/scale; VARCHAR2(n CHAR) → length/charSemantics; TIMESTAMP(p)."""
    if not type_str:
        return {"dataType": type_str}
    m = re.match(r"^(\w+(?:\s+\w+)*?)\s*(?:\(([^)]+)\))?$", type_str, re.IGNORECASE)
    if not m:
        return {"dataType": type_str.upper()}
    type_name = re.sub(r"\s+", " ", m.group(1).upper())
    params = m.group(2).strip() if m.group(2) else None
    length = precision = scale = char_semantics = None
    if params:
        if type_name in ("NUMBER", "FLOAT", "DECIMAL", "NUMERIC"):
            parts = [p.strip() for p in params.split(",")]
            precision = _to_int(parts[0])
            scale = (_to_int(parts[1]) or 0) if len(parts) > 1 else None
        elif type_name in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW"):
            cm = re.match(r"^(\d+)\s*(CHAR|BYTE)?$", params, re.IGNORECASE)
            if cm:
                length = _to_int(cm.group(1))
                char_semantics = cm.group(2).upper() if cm.group(2) else None
        elif type_name in ("TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH LOCAL TIME ZONE"):
            precision = _to_int(params)
        else:
            length = _to_int(params)
    return {
        "dataType": type_name,
        "length": length,
        "precision": precision,
        "scale": scale,
        "charSemantics": char_semantics,
    }


def _extract_default(fragment: str) -> dict[str, Any] | None:
    """Extract a ``DEFAULT <expr>`` from a column fragment, paren/string-aware, stopping at
    the next constraint keyword. Tags the value literal/function_call/pseudo_column/expression."""
    m = re.search(r"\bDEFAULT\b", fragment, re.IGNORECASE)
    if not m:
        return None
    i = m.end()
    while i < len(fragment) and fragment[i].isspace():
        i += 1
    on_null = re.match(r"^ON\s+NULL\s*", fragment[i:], re.IGNORECASE)  # 12c: DEFAULT ON NULL
    if on_null:
        i += on_null.end()
    stop = re.compile(
        r"^(NOT\s+NULL|NULL|CONSTRAINT|CHECK|UNIQUE|PRIMARY\s+KEY|REFERENCES|ENABLE|"
        r"DISABLE|VISIBLE|INVISIBLE|ENCRYPT|GENERATED)\b",
        re.IGNORECASE,
    )
    depth = 0
    in_str = False
    buf = ""
    while i < len(fragment):
        ch = fragment[i]
        if in_str:
            buf += ch
            i += 1
            if ch == "'":
                if i < len(fragment) and fragment[i] == "'":
                    buf += "'"
                    i += 1
                else:
                    in_str = False
            continue
        if ch == "'":
            in_str = True
            buf += ch
            i += 1
            continue
        if ch == "(":
            depth += 1
            buf += ch
            i += 1
            continue
        if ch == ")":
            if depth == 0:
                break
            depth -= 1
            buf += ch
            i += 1
            continue
        if depth == 0 and ch.isspace() and stop.match(fragment[i + 1 :]):
            break
        buf += ch
        i += 1
    value = re.sub(r",$", "", buf.strip())
    if not value:
        return None
    if re.match(r"^'.*'$", value) or re.match(r"^N'.*'$", value, re.IGNORECASE):
        kind = "literal"
    elif re.match(r"^-?\d+(\.\d+)?$", value):
        kind = "literal"
    elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*\(", value):
        kind = "function_call"
    elif re.match(
        r"^(SYSDATE|SYSTIMESTAMP|CURRENT_DATE|CURRENT_TIMESTAMP|USER|UID|NULL|TRUE|FALSE)$",
        value,
        re.IGNORECASE,
    ):
        kind = "pseudo_column"
    else:
        kind = "expression"
    return {"value": value, "kind": kind}


def _extract_inline_check(fragment: str) -> str | None:
    m = re.search(r"\bCHECK\s*\((.+)\)", fragment, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _parse_column_def(def_str: str, table_name: str) -> dict[str, Any] | None:
    """Parse one column definition into ``{column, inlineConstraints}``. Returns None for a
    table-level constraint line (routed to _parse_table_constraint) or a non-column clause."""
    d = def_str.strip()
    if re.match(r"^(CONSTRAINT|PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK)\b", d, re.IGNORECASE):
        return None
    if re.match(r"^(SUPPLEMENTAL\s+LOG\b|PERIOD\s+FOR\b)", d, re.IGNORECASE):
        return None
    name_match = re.match(r"^(" + _IDENT + r")\s+([\s\S]+)", d)
    if not name_match:
        return None
    name = _unquote_ident(name_match.group(1))
    rest = name_match.group(2)

    type_end = re.match(
        r"^([^(,]+(?:\([^)]*\))?(?:\s+(?:WITH\s+TIME\s+ZONE|WITH\s+LOCAL\s+TIME\s+ZONE))?)",
        rest,
        re.IGNORECASE,
    )
    type_str = type_end.group(0).strip() if type_end else rest.strip()
    type_str = re.sub(
        r"\s+(NOT\s+NULL|NULL|DEFAULT|CONSTRAINT|PRIMARY|UNIQUE|CHECK|REFERENCES|GENERATED|"
        r"ENABLE|DISABLE|VISIBLE|INVISIBLE|ENCRYPT|AS\b).*$",
        "",
        type_str,
        flags=re.IGNORECASE,
    ).strip()
    dt = _parse_data_type(type_str)

    nullable = not re.search(r"\bNOT\s+NULL\b", rest, re.IGNORECASE)
    is_pk = bool(re.search(r"\bPRIMARY\s+KEY\b", rest, re.IGNORECASE))
    is_unique = bool(re.search(r"\bUNIQUE\b", rest, re.IGNORECASE))
    default_info = _extract_default(rest)
    check_expr = _extract_inline_check(rest)

    ident_match = re.search(
        r"\bGENERATED\s+(ALWAYS|BY\s+DEFAULT(?:\s+ON\s+NULL)?)\s+AS\s+IDENTITY\b", rest, re.IGNORECASE
    )
    is_identity = bool(ident_match)
    identity_generation = re.sub(r"\s+", " ", ident_match.group(1).upper()) if ident_match else None

    virt_match = re.search(
        r"\bGENERATED\s+ALWAYS\s+AS\s*\(([\s\S]+?)\)\s*(?:VIRTUAL)?(?!\s+IDENTITY)", rest, re.IGNORECASE
    ) or re.search(r"(?:^|\s)AS\s*\(([\s\S]+?)\)\s*VIRTUAL\b", rest, re.IGNORECASE)
    is_virtual = bool(virt_match) and not is_identity

    col: dict[str, Any] = {
        "name": name,
        "dataType": dt.get("dataType") or "UNKNOWN",
        "nullable": nullable,
        "isPrimaryKey": is_pk,
        "isUnique": is_unique,
        "isForeignKey": False,
        "isIndexed": False,
    }
    if dt.get("length") is not None:
        col["length"] = dt["length"]
    if dt.get("precision") is not None:
        col["precision"] = dt["precision"]
    if dt.get("scale") is not None:
        col["scale"] = dt["scale"]
    if dt.get("charSemantics"):
        col["charSemantics"] = dt["charSemantics"]
    if default_info:
        col["defaultValue"] = default_info["value"]
        col["defaultKind"] = default_info["kind"]
    if check_expr:
        col["checkExpression"] = check_expr
    if is_identity:
        col["isIdentity"] = True
        col["identityGeneration"] = identity_generation
    if is_virtual:
        col["isVirtual"] = True
        col["virtualExpression"] = virt_match.group(1).strip()

    inline_constraints: list[dict[str, Any]] = []
    fk_m = re.search(
        r"(?:CONSTRAINT\s+(" + _IDENT + r")\s+)?REFERENCES\s+(?:(" + _IDENT + r")\.)?(" + _IDENT + r")"
        r"\s*(?:\(([^)]+)\))?(?:\s+ON\s+DELETE\s+(CASCADE|SET\s+NULL|SET\s+DEFAULT|NO\s+ACTION|RESTRICT))?",
        rest,
        re.IGNORECASE,
    )
    if fk_m:
        c: dict[str, Any] = {
            "name": _unquote_ident(fk_m.group(1)) if fk_m.group(1) else None,
            "tableName": table_name,
            "constraintType": "FOREIGN_KEY",
            "columns": [name],
            "refTableName": _unquote_ident(fk_m.group(3)),
            "refColumns": _split_col_list(fk_m.group(4)) if fk_m.group(4) else [],
        }
        if fk_m.group(2):
            c["refTableSchema"] = _unquote_ident(fk_m.group(2))
        if fk_m.group(5):
            c["onDelete"] = re.sub(r"\s+", " ", fk_m.group(5).upper())
        inline_constraints.append(c)
        col["isForeignKey"] = True

    pk_m = re.search(r"(?:CONSTRAINT\s+(" + _IDENT + r")\s+)?PRIMARY\s+KEY\b", rest, re.IGNORECASE)
    if pk_m:
        inline_constraints.append(
            {
                "name": _unquote_ident(pk_m.group(1)) if pk_m.group(1) else None,
                "tableName": table_name,
                "constraintType": "PRIMARY_KEY",
                "columns": [name],
            }
        )

    uq_m = re.search(r"(?:CONSTRAINT\s+(" + _IDENT + r")\s+)?UNIQUE(?!\s+INDEX)\b", rest, re.IGNORECASE)
    if uq_m and not pk_m:
        inline_constraints.append(
            {
                "name": _unquote_ident(uq_m.group(1)) if uq_m.group(1) else None,
                "tableName": table_name,
                "constraintType": "UNIQUE",
                "columns": [name],
            }
        )

    if check_expr:
        ck_name_m = re.search(r"CONSTRAINT\s+(" + _IDENT + r")\s+", rest, re.IGNORECASE)
        inline_constraints.append(
            {
                "name": _unquote_ident(ck_name_m.group(1)) if ck_name_m else None,
                "tableName": table_name,
                "constraintType": "CHECK",
                "columns": [],
                "checkExpression": check_expr,
            }
        )

    return {"column": col, "inlineConstraints": inline_constraints}


def _parse_table_constraint(def_str: str, table_name: str) -> dict[str, Any] | None:
    """Parse a table-level PRIMARY KEY / UNIQUE / FOREIGN KEY / CHECK constraint definition
    (with an optional leading ``CONSTRAINT name``)."""
    d = def_str.strip()
    constraint_name = None
    named = re.match(r"^CONSTRAINT\s+(" + _IDENT + r")\s+", d, re.IGNORECASE)
    if named:
        constraint_name = _unquote_ident(named.group(1))
        d = d[named.end() :]

    con: dict[str, Any] = {"name": constraint_name, "tableName": table_name}

    pk = re.match(r"^PRIMARY\s+KEY\s*\(([^)]+)\)", d, re.IGNORECASE)
    if pk:
        con["constraintType"] = "PRIMARY_KEY"
        con["columns"] = _split_col_list(pk.group(1))
        return con

    uq = re.match(r"^UNIQUE\s*\(([^)]+)\)", d, re.IGNORECASE)
    if uq:
        con["constraintType"] = "UNIQUE"
        con["columns"] = _split_col_list(uq.group(1))
        return con

    fk = re.match(
        r"^FOREIGN\s+KEY\s*\(([^)]+)\)\s+REFERENCES\s+(?:(" + _IDENT + r")\.)?(" + _IDENT + r")"
        r"\s*(?:\(([^)]+)\))?(?:\s+ON\s+DELETE\s+(CASCADE|SET\s+NULL|SET\s+DEFAULT|NO\s+ACTION|RESTRICT))?",
        d,
        re.IGNORECASE,
    )
    if fk:
        con["constraintType"] = "FOREIGN_KEY"
        con["columns"] = _split_col_list(fk.group(1))
        if fk.group(2):
            con["refTableSchema"] = _unquote_ident(fk.group(2))
        con["refTableName"] = _unquote_ident(fk.group(3))
        con["refColumns"] = _split_col_list(fk.group(4)) if fk.group(4) else []
        if fk.group(5):
            con["onDelete"] = re.sub(r"\s+", " ", fk.group(5).upper())
        if re.search(r"\bDEFERRABLE\b", d, re.IGNORECASE):
            con["deferrable"] = True
        if re.search(r"\bINITIALLY\s+DEFERRED\b", d, re.IGNORECASE):
            con["initiallyDeferred"] = True
        return con

    ck = re.match(
        r"^CHECK\s*\((.+)\)(?:\s+ENABLE)?(?:\s+DISABLE)?(?:\s+VALIDATE)?(?:\s+NOVALIDATE)?",
        d,
        re.IGNORECASE,
    )
    if ck:
        con["constraintType"] = "CHECK"
        con["columns"] = []
        con["checkExpression"] = ck.group(1).strip()
        en = re.search(r"\b(ENABLE|DISABLE)\b", d, re.IGNORECASE)
        con["enabled"] = (en.group(1).upper() == "ENABLE") if en else True
        val = re.search(r"\b(VALIDATE|NOVALIDATE)\b", d, re.IGNORECASE)
        con["validated"] = (val.group(1).upper() == "VALIDATE") if val else True
        return con

    return None


def _parse_create_table(stmt: str) -> dict[str, Any] | None:
    """Parse ``CREATE [GLOBAL TEMPORARY] TABLE [schema.]name (body)`` into a table record."""
    header = re.match(
        r"^CREATE\s+(?:GLOBAL\s+TEMPORARY\s+)?TABLE\s+(?:(" + _IDENT + r")\.)?(" + _IDENT + r")\s*\(",
        stmt,
        re.IGNORECASE,
    )
    if not header:
        return None
    schema = _unquote_ident(header.group(1)) if header.group(1) else None
    name = _unquote_ident(header.group(2))
    full = f"{schema}.{name}" if schema else name

    body_start = stmt.find("(")
    body_end = _find_matching_paren(stmt, body_start)
    if body_start == -1 or body_end == -1:
        return None
    body = stmt[body_start + 1 : body_end]
    table_type = "temporary" if re.search(r"GLOBAL\s+TEMPORARY", stmt, re.IGNORECASE) else "table"

    columns: list[dict[str, Any]] = []
    constraints: list[dict[str, Any]] = []
    for d in _split_paren_aware(body):
        if re.match(r"^(CONSTRAINT\b|PRIMARY\s+KEY\b|UNIQUE\b|FOREIGN\s+KEY\b|CHECK\b)", d.strip(), re.IGNORECASE):
            c = _parse_table_constraint(d, name)
            if c:
                constraints.append(c)
        else:
            res = _parse_column_def(d, name)
            if res:
                columns.append(res["column"])
                constraints.extend(res["inlineConstraints"])

    pk_con = next((c for c in constraints if c["constraintType"] == "PRIMARY_KEY"), None)
    if pk_con:
        for col in columns:
            if col["name"] in pk_con["columns"]:
                col["isPrimaryKey"] = True
    for con in constraints:
        if con["constraintType"] == "FOREIGN_KEY":
            for col in columns:
                if col["name"] in con["columns"]:
                    col["isForeignKey"] = True

    ddl_text = stmt[:1000] + ("\n-- [truncated]" if len(stmt) > 1000 else "")
    return {
        "name": name,
        "schema": schema,
        "fullName": full,
        "tableType": table_type,
        "columnCount": len(columns),
        "hasPrimaryKey": any(c["isPrimaryKey"] for c in columns) or bool(pk_con),
        "columns": columns,
        "constraints": constraints,
        "ddlText": ddl_text,
    }


def _parse_create_view(stmt: str) -> dict[str, Any] | None:
    m = re.match(
        r"^CREATE\s+(?:OR\s+REPLACE\s+)?(?:FORCE\s+|NOFORCE\s+)?(?:MATERIALIZED\s+)?VIEW\s+"
        r"(?:(" + _IDENT + r")\.)?(" + _IDENT + r")([\s\S]*?)\bAS\b\s+([\s\S]+)",
        stmt,
        re.IGNORECASE,
    )
    if not m:
        return None
    schema = _unquote_ident(m.group(1)) if m.group(1) else None
    name = _unquote_ident(m.group(2))
    full = f"{schema}.{name}" if schema else name
    col_list_m = re.match(r"^\s*\(([^)]*)\)", m.group(3) or "")
    column_list = _split_col_list(col_list_m.group(1)) if col_list_m else []
    definition = m.group(4).strip()[:1000] if m.group(4) else None
    is_mat = bool(re.search(r"MATERIALIZED\s+VIEW", stmt, re.IGNORECASE))
    return {
        "name": name,
        "schema": schema,
        "fullName": full,
        "viewType": "materialized_view" if is_mat else "view",
        "definition": definition,
        "columns": column_list,
    }


def _parse_create_index(stmt: str) -> dict[str, Any] | None:
    m = re.match(
        r"^CREATE\s+(UNIQUE\s+|BITMAP\s+)?INDEX\s+(?:(" + _IDENT + r")\.)?(" + _IDENT + r")\s+ON\s+"
        r"(?:(" + _IDENT + r")\.)?(" + _IDENT + r")\s*\(([^)]+)\)(?:\s+([\s\S]*))?",
        stmt,
        re.IGNORECASE,
    )
    if not m:
        return None
    index_modifier = m.group(1).strip().upper() if m.group(1) else None
    index_name = _unquote_ident(m.group(3))
    table_schema = _unquote_ident(m.group(4)) if m.group(4) else None
    table_name = _unquote_ident(m.group(5))
    table_full = f"{table_schema}.{table_name}" if table_schema and table_name else table_name
    col_str = m.group(6)
    options = m.group(7) or ""

    is_function_based = bool(re.search(r"[()+\-*/]", col_str))
    columns = [] if is_function_based else _split_col_list(col_str)
    expressions = [col_str.strip()] if is_function_based else None

    index_type = "BTREE"
    if index_modifier == "BITMAP":
        index_type = "BITMAP"
    elif is_function_based:
        index_type = "FUNCTION_BASED"

    ts = re.search(r"\bTABLESPACE\s+(\w+)", options, re.IGNORECASE)
    out: dict[str, Any] = {
        "name": index_name,
        "tableName": table_name,
        "tableFullName": table_full,
        "columns": columns,
        "isUnique": index_modifier == "UNIQUE",
        "indexType": index_type,
        "tablespace": ts.group(1).upper() if ts else None,
    }
    if expressions:
        out["expressions"] = expressions
    return out


def _parse_comment(stmt: str) -> dict[str, Any] | None:
    tbl = re.match(
        r"^COMMENT\s+ON\s+(?:TABLE|VIEW|MATERIALIZED\s+VIEW)\s+(?:(" + _IDENT + r")\.)?(" + _IDENT + r")"
        r"\s+IS\s+'((?:[^']|'')*)'",
        stmt,
        re.IGNORECASE,
    )
    if tbl:
        return {
            "target": "table",
            "schema": _unquote_ident(tbl.group(1)) if tbl.group(1) else None,
            "name": _unquote_ident(tbl.group(2)),
            "comment": tbl.group(3).replace("''", "'"),
        }
    col = re.match(
        r"^COMMENT\s+ON\s+COLUMN\s+(?:(" + _IDENT + r")\.)?(" + _IDENT + r")\.(" + _IDENT + r")"
        r"\s+IS\s+'((?:[^']|'')*)'",
        stmt,
        re.IGNORECASE,
    )
    if col:
        return {
            "target": "column",
            "schema": _unquote_ident(col.group(1)) if col.group(1) else None,
            "tableName": _unquote_ident(col.group(2)),
            "columnName": _unquote_ident(col.group(3)),
            "comment": col.group(4).replace("''", "'"),
        }
    return None


def _parse_alter_table_constraint(stmt: str) -> dict[str, Any] | None:
    m = re.match(
        r"^ALTER\s+TABLE\s+(?:(" + _IDENT + r")\.)?(" + _IDENT + r")\s+ADD\s+([\s\S]*)", stmt, re.IGNORECASE
    )
    if not m:
        return None
    con = _parse_table_constraint(m.group(3).strip(), _unquote_ident(m.group(2)))
    if not con:
        return None
    return {
        "tableName": _unquote_ident(m.group(2)),
        "schema": _unquote_ident(m.group(1)) if m.group(1) else None,
        "constraint": con,
    }


def _parse_alter_table_drop_constraint(stmt: str) -> dict[str, Any] | None:
    m = re.match(
        r"^ALTER\s+TABLE\s+(?:(" + _IDENT + r")\.)?(" + _IDENT + r")\s+DROP\s+CONSTRAINT\s+(" + _IDENT + r")",
        stmt,
        re.IGNORECASE,
    )
    if not m:
        return None
    return {
        "schema": _unquote_ident(m.group(1)) if m.group(1) else None,
        "tableName": _unquote_ident(m.group(2)),
        "constraintName": _unquote_ident(m.group(3)),
    }


def _parse_alter_table_add_column(stmt: str) -> dict[str, Any] | None:
    m = re.match(
        r"^ALTER\s+TABLE\s+(?:ONLY\s+)?(?:(" + _IDENT + r")\.)?(" + _IDENT + r")\s+ADD\s+([\s\S]+)",
        stmt,
        re.IGNORECASE,
    )
    if not m:
        return None
    schema = _unquote_ident(m.group(1)) if m.group(1) else None
    table_name = _unquote_ident(m.group(2))
    add_body = m.group(3).strip()
    if re.match(r"^(CONSTRAINT\b|PRIMARY\s+KEY\b|UNIQUE\b|FOREIGN\s+KEY\b|CHECK\b)", add_body, re.IGNORECASE):
        return None
    if re.match(r"^(SUPPLEMENTAL\s+LOG\b|PERIOD\s+FOR\b)", add_body, re.IGNORECASE):
        return None
    if add_body.startswith("("):
        end = _find_matching_paren(add_body, 0)
        if end != -1:
            add_body = add_body[1:end]

    columns: list[dict[str, Any]] = []
    inline_constraints: list[dict[str, Any]] = []
    for d in _split_paren_aware(add_body):
        t = d.strip()
        if re.match(r"^(CONSTRAINT\b|PRIMARY\s+KEY\b|UNIQUE\b|FOREIGN\s+KEY\b|CHECK\b)", t, re.IGNORECASE):
            con = _parse_table_constraint(d, table_name)
            if con:
                inline_constraints.append(con)
            continue
        if re.match(r"^(SUPPLEMENTAL\s+LOG\b|PERIOD\s+FOR\b)", t, re.IGNORECASE):
            continue
        res = _parse_column_def(d, table_name)
        if res:
            columns.append(res["column"])
            inline_constraints.extend(res["inlineConstraints"])

    if not columns and not inline_constraints:
        return None
    return {"schema": schema, "tableName": table_name, "columns": columns, "inlineConstraints": inline_constraints}


def _parse_alter_table_modify_column(stmt: str) -> dict[str, Any] | None:
    m = re.match(
        r"^ALTER\s+TABLE\s+(?:ONLY\s+)?(?:(" + _IDENT + r")\.)?(" + _IDENT + r")\s+MODIFY\s+([\s\S]+)",
        stmt,
        re.IGNORECASE,
    )
    if not m:
        return None
    mod_body = m.group(3).strip()
    if mod_body.startswith("("):
        end = _find_matching_paren(mod_body, 0)
        if end != -1:
            mod_body = mod_body[1:end]
    mods = []
    for d in _split_paren_aware(mod_body):
        res = _parse_column_def(d, _unquote_ident(m.group(2)))
        if res:
            mods.append(res["column"])
    if not mods:
        return None
    return {
        "schema": _unquote_ident(m.group(1)) if m.group(1) else None,
        "tableName": _unquote_ident(m.group(2)),
        "modifications": mods,
    }


def _parse_alter_table_drop_column(stmt: str) -> dict[str, Any] | None:
    m1 = re.match(
        r"^ALTER\s+TABLE\s+(?:ONLY\s+)?(?:(" + _IDENT + r")\.)?(" + _IDENT + r")\s+DROP\s+COLUMN\s+(" + _IDENT + r")",
        stmt,
        re.IGNORECASE,
    )
    if m1:
        return {
            "schema": _unquote_ident(m1.group(1)) if m1.group(1) else None,
            "tableName": _unquote_ident(m1.group(2)),
            "columns": [_unquote_ident(m1.group(3))],
        }
    m2 = re.match(
        r"^ALTER\s+TABLE\s+(?:ONLY\s+)?(?:(" + _IDENT + r")\.)?(" + _IDENT + r")\s+DROP\s+\(([^)]+)\)",
        stmt,
        re.IGNORECASE,
    )
    if m2:
        return {
            "schema": _unquote_ident(m2.group(1)) if m2.group(1) else None,
            "tableName": _unquote_ident(m2.group(2)),
            "columns": _split_col_list(m2.group(3)),
        }
    return None


def _parse_alter_table_rename_column(stmt: str) -> dict[str, Any] | None:
    m = re.match(
        r"^ALTER\s+TABLE\s+(?:ONLY\s+)?(?:(" + _IDENT + r")\.)?(" + _IDENT + r")\s+RENAME\s+COLUMN\s+"
        r"(" + _IDENT + r")\s+TO\s+(" + _IDENT + r")",
        stmt,
        re.IGNORECASE,
    )
    if not m:
        return None
    return {
        "schema": _unquote_ident(m.group(1)) if m.group(1) else None,
        "tableName": _unquote_ident(m.group(2)),
        "oldName": _unquote_ident(m.group(3)),
        "newName": _unquote_ident(m.group(4)),
    }


def _same_col_list(a: list | None, b: list | None) -> bool:
    return sorted(str(x).upper() for x in (a or [])) == sorted(str(x).upper() for x in (b or []))


def _constraints_equivalent(a: dict | None, b: dict | None) -> bool:
    """Semantic constraint equality (name-agnostic) so an inline FK re-declared via ALTER
    isn't double-counted."""
    if not a or not b:
        return False
    if a.get("constraintType") != b.get("constraintType"):
        return False
    if not _same_col_list(a.get("columns"), b.get("columns")):
        return False
    if a.get("constraintType") == "FOREIGN_KEY":
        if (a.get("refTableName") or "").upper() != (b.get("refTableName") or "").upper():
            return False
        if not _same_col_list(a.get("refColumns"), b.get("refColumns")):
            return False
    return True


def _recompute_column_flags(tbl: dict[str, Any]) -> None:
    for col in tbl["columns"]:
        col["isPrimaryKey"] = False
        col["isForeignKey"] = False
    tbl["hasPrimaryKey"] = False
    for c in tbl["constraints"]:
        if c["constraintType"] == "PRIMARY_KEY":
            tbl["hasPrimaryKey"] = True
            for col in tbl["columns"]:
                if c.get("columns") and col["name"] in c["columns"]:
                    col["isPrimaryKey"] = True
        if c["constraintType"] == "FOREIGN_KEY":
            for col in tbl["columns"]:
                if c.get("columns") and col["name"] in c["columns"]:
                    col["isForeignKey"] = True


def _apply_oracle_alter(stmt: str, table_map: dict[str, dict], comment_map: dict[str, dict]) -> bool:
    """Apply one ALTER TABLE mutation (DROP/RENAME/ADD/MODIFY column, ADD/DROP constraint) to
    the owning table in file order. Returns True if the statement was recognized."""

    def find_table(name: str, schema: str | None) -> dict | None:
        return table_map.get(name) or (table_map.get(f"{schema}.{name}") if schema else None)

    dc = _parse_alter_table_drop_constraint(stmt)
    if dc:
        tbl = find_table(dc["tableName"], dc["schema"])
        if tbl:
            tbl["constraints"] = [c for c in tbl["constraints"] if c.get("name") != dc["constraintName"]]
            _recompute_column_flags(tbl)
        return True

    dcol = _parse_alter_table_drop_column(stmt)
    if dcol:
        tbl = find_table(dcol["tableName"], dcol["schema"])
        if tbl:
            tbl["columns"] = [c for c in tbl["columns"] if c["name"] not in dcol["columns"]]
            tbl["constraints"] = [
                c
                for c in tbl["constraints"]
                if not (c.get("columns") and any(col in dcol["columns"] for col in c["columns"]))
            ]
            tbl["columnCount"] = len(tbl["columns"])
            for i, c in enumerate(tbl["columns"]):
                c["ordinalPosition"] = i + 1
            _recompute_column_flags(tbl)
        return True

    rc = _parse_alter_table_rename_column(stmt)
    if rc:
        tbl = find_table(rc["tableName"], rc["schema"])
        if tbl:
            for col in tbl["columns"]:
                if col["name"] == rc["oldName"]:
                    col["name"] = rc["newName"]
            for con in tbl["constraints"]:
                if con.get("columns"):
                    con["columns"] = [rc["newName"] if c == rc["oldName"] else c for c in con["columns"]]
            ck = f'{tbl["name"]}.{rc["oldName"]}'
            if ck in comment_map["columns"]:
                comment_map["columns"][f'{tbl["name"]}.{rc["newName"]}'] = comment_map["columns"].pop(ck)
        return True

    ac = _parse_alter_table_add_column(stmt)
    if ac:
        tbl = find_table(ac["tableName"], ac["schema"])
        if tbl:
            for new_col in ac["columns"]:
                if not any(c["name"] == new_col["name"] for c in tbl["columns"]):
                    new_col["ordinalPosition"] = len(tbl["columns"]) + 1
                    tbl["columns"].append(new_col)
            if ac["inlineConstraints"]:
                tbl["constraints"].extend(ac["inlineConstraints"])
            tbl["columnCount"] = len(tbl["columns"])
            _recompute_column_flags(tbl)
        return True

    mc = _parse_alter_table_modify_column(stmt)
    if mc:
        tbl = find_table(mc["tableName"], mc["schema"])
        if tbl:
            for mod in mc["modifications"]:
                existing = next((c for c in tbl["columns"] if c["name"] == mod["name"]), None)
                if not existing:
                    continue
                if mod.get("dataType") and mod["dataType"] != "UNKNOWN":
                    existing["dataType"] = mod["dataType"]
                if mod.get("length") is not None:
                    existing["length"] = mod["length"]
                if mod.get("precision") is not None:
                    existing["precision"] = mod["precision"]
                if mod.get("scale") is not None:
                    existing["scale"] = mod["scale"]
                if mod.get("charSemantics"):
                    existing["charSemantics"] = mod["charSemantics"]
                existing["nullable"] = mod["nullable"]
                if "defaultValue" in mod:
                    existing["defaultValue"] = mod["defaultValue"]
                    existing["defaultKind"] = mod.get("defaultKind")
                if mod.get("isIdentity"):
                    existing["isIdentity"] = True
                    existing["identityGeneration"] = mod.get("identityGeneration")
                if mod.get("isVirtual"):
                    existing["isVirtual"] = True
                    existing["virtualExpression"] = mod.get("virtualExpression")
        return True

    alt = _parse_alter_table_constraint(stmt)
    if alt:
        tbl = find_table(alt["tableName"], alt["schema"])
        if tbl:
            if not any(_constraints_equivalent(c, alt["constraint"]) for c in tbl["constraints"]):
                tbl["constraints"].append(alt["constraint"])
                _recompute_column_flags(tbl)
        return True

    return False


def _parse_oracle_ddl(text: str) -> dict[str, Any]:
    """Full Oracle DDL parser (port of parseOracleDDL in the JS Oracle parser). Splits the
    (SQL*Plus-stripped) text into statements, routes each CREATE/COMMENT/ALTER, applies
    comments + ALTER mutations in file order, wires indexes, and returns the standard
    ``parse_ddl`` payload (minus ``dialect``, which the caller adds)."""
    tables: list[dict[str, Any]] = []
    views: list[dict[str, Any]] = []
    procedures: list[dict[str, Any]] = []
    indexes: list[dict[str, Any]] = []
    sequences: list[dict[str, Any]] = []
    comment_map: dict[str, dict] = {"tables": {}, "columns": {}}
    table_map: dict[str, dict] = {}
    parsed = 0
    skipped = 0
    skipped_samples: list[str] = []

    def record_skip(stmt: str, reason: str) -> None:
        nonlocal skipped
        skipped += 1
        if len(skipped_samples) < 5:
            skipped_samples.append(f"{reason}: {stmt[:80]}")

    for stmt in _split_statements(text):
        stripped = re.sub(r"^(\s*--[^\n]*\n)+\s*", "", stmt).lstrip()
        upper = stripped.upper()

        if upper.startswith("CREATE") and re.search(r"CREATE\s+(?:GLOBAL\s+TEMPORARY\s+)?TABLE\b", stripped, re.IGNORECASE):
            t = _parse_create_table(stripped)
            if t:
                tables.append(t)
                table_map[t["name"]] = t
                if t["schema"]:
                    table_map[t["fullName"]] = t
                parsed += 1
            else:
                record_skip(stripped, "create_table_unparsed")
        elif upper.startswith("CREATE") and re.search(
            r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:FORCE\s+|NOFORCE\s+)?(?:MATERIALIZED\s+)?VIEW\b", stripped, re.IGNORECASE
        ):
            v = _parse_create_view(stripped)
            if v:
                views.append(v)
                parsed += 1
            else:
                record_skip(stripped, "create_view_unparsed")
        elif upper.startswith("CREATE") and re.search(r"CREATE\s+SEQUENCE\b", stripped, re.IGNORECASE):
            s = _parse_create_sequence(stripped)
            if s:
                sequences.append(s)
                parsed += 1
            else:
                record_skip(stripped, "create_sequence_unparsed")
        elif upper.startswith("CREATE") and _PROC_HEAD_RE.match(stripped):
            p = _parse_create_procedure(stripped)
            if p:
                procedures.append(p)
                parsed += 1
            else:
                record_skip(stripped, "create_procedure_unparsed")
        elif upper.startswith("CREATE") and re.search(r"CREATE\s+(?:UNIQUE\s+|BITMAP\s+)?INDEX\b", stripped, re.IGNORECASE):
            idx = _parse_create_index(stripped)
            if idx:
                indexes.append(idx)
                parsed += 1
            else:
                record_skip(stripped, "create_index_unparsed")
        elif upper.startswith("COMMENT"):
            c = _parse_comment(stripped)
            if c:
                if c["target"] == "table":
                    comment_map["tables"][c["name"]] = c["comment"]
                    if c["schema"]:
                        comment_map["tables"][f'{c["schema"]}.{c["name"]}'] = c["comment"]
                else:
                    comment_map["columns"][f'{c["tableName"]}.{c["columnName"]}'] = c["comment"]
                parsed += 1
            else:
                record_skip(stripped, "comment_unparsed")
        elif upper.startswith("ALTER"):
            if _apply_oracle_alter(stripped, table_map, comment_map):
                parsed += 1
            else:
                record_skip(stripped, "alter_unparsed")
        else:
            record_skip(stripped, "unrecognized_statement")

    for table in tables:
        tc = comment_map["tables"].get(table["name"]) or comment_map["tables"].get(table["fullName"])
        if tc:
            table["comment"] = tc
        for col in table["columns"]:
            cc = comment_map["columns"].get(f'{table["name"]}.{col["name"]}')
            if cc:
                col["comment"] = cc

    for idx in indexes:
        table = table_map.get(idx["tableName"]) or table_map.get(idx["tableFullName"])
        if table:
            for col in table["columns"]:
                if col["name"] in idx["columns"]:
                    col["isIndexed"] = True
            table.setdefault("indexes", []).append(idx)
    for table in tables:
        table.setdefault("indexes", [])

    for table in tables:
        for i, col in enumerate(table["columns"]):
            col["ordinalPosition"] = i + 1
        table["columnCount"] = len(table["columns"])

    standalone = [
        idx for idx in indexes if not (table_map.get(idx["tableName"]) or table_map.get(idx["tableFullName"]))
    ]

    # Deterministic ordering: constraints/indexes within a table sort by (type, cols, name)
    # so a re-parse yields byte-identical output and diffs reflect real schema changes.
    def ckey(c: dict) -> str:
        return "|".join([c.get("constraintType") or "", ",".join(c.get("columns") or []), c.get("name") or ""])

    def ikey(i: dict) -> str:
        return "|".join([i.get("indexType") or "", ",".join(i.get("columns") or []), i.get("name") or ""])

    for t in tables:
        t.get("constraints", []).sort(key=ckey)
        t.get("indexes", []).sort(key=ikey)

    return {
        "tables": tables,
        "views": views,
        "procedures": procedures,
        "allIndexes": indexes,
        "indexes": standalone,
        "sequences": sequences,
        "parseStats": {"ok": parsed, "failed": skipped, "sampleErrors": skipped_samples},
    }
