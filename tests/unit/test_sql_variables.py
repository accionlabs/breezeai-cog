"""SQL built up in a local over several statements: one query, at the statement that runs it."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.detection.queries import (
    built_string_has_query,
    is_sql_command_type,
    is_sql_sink,
    sql_argument,
)


def _parser(lang: str):
    from breezeai_cog.parsers.csharp.parser import CSharpParser
    from breezeai_cog.parsers.java.parser import JavaParser
    from breezeai_cog.parsers.php.parser import PhpParser
    from breezeai_cog.parsers.python.parser import PythonParser
    from breezeai_cog.parsers.typescript.parser import TypeScriptParser
    from breezeai_cog.parsers.vb.parser import VbParser

    return {"php": PhpParser, "typescript": TypeScriptParser, "javascript": TypeScriptParser,
            "java": JavaParser, "csharp": CSharpParser, "python": PythonParser, "vb": VbParser}[lang]()


_FILES = {"python": "m.py", "csharp": "M.cs", "java": "M.java", "php": "m.php", "vb": "M.vb",
          "typescript": "m.ts", "javascript": "m.js"}


def _queries(lang: str, src: str, tmp_path) -> list[tuple[int, str | None]]:
    """``(line, method)`` of every query_statement in ``src``."""
    name = _FILES[lang]
    ctx = ParseContext(path=name, abs_path=Path(tmp_path) / name, source=src.encode(),
                       repo_root=Path(tmp_path), capture_statements=True,
                       statement_text_limit=1000)
    rec = _parser(lang).parse_file(ctx)
    return sorted({(s.startLine, s.method) for s in rec.statements
                   if s.semanticType == "query_statement"})


# ── each language: the statement that runs the built SQL is the one query ───────────────
_BUILT = [
    ("python", "augmented", """\
def load(cur, active_only):
    sql = "SELECT id, total "
    sql += "FROM orders"
    if active_only:
        sql += " WHERE active = 1"
    cur.execute(sql)
""", [(6, "execute")]),
    ("python", "local constant", """\
def load(cur):
    table = "orders"
    sql = "SELECT id "
    sql = sql + "FROM " + table
    cur.execute(sql)
""", [(5, "execute")]),
    ("csharp", "command constructor", """\
class R {
    void Load(SqlConnection conn, bool activeOnly) {
        var sql = "SELECT id, total ";
        sql += "FROM orders";
        if (activeOnly) sql += " WHERE active = 1";
        var cmd = new SqlCommand(sql, conn);
        cmd.ExecuteReader();
    }
}
""", [(6, None)]),
    ("csharp", "dapper db call", """\
class R {
    void Del(SqlConnection conn) {
        string sql = "DELETE ";
        sql = sql + "FROM orders WHERE id = @id";
        conn.Execute(sql);
    }
}
""", [(5, "Execute")]),
    ("csharp", "dapper sink name", """\
class R {
    void Get(SqlConnection conn) {
        var sql = "SELECT a ";
        sql += "FROM t";
        conn.QueryAsync<int>(sql);
    }
}
""", [(5, "QueryAsync")]),
    ("csharp", "builder to local", """\
class R {
    void B(SqlConnection c) {
        var sb = new StringBuilder();
        sb.AppendLine("SELECT a").AppendLine("FROM t");
        string sql = sb.ToString();
        var cmd = new SqlCommand(sql, c);
    }
}
""", [(6, None)]),
    ("csharp", "builder inline", """\
class R {
    void B(SqlConnection c) {
        var sb = new StringBuilder("SELECT id ");
        sb.Append("FROM orders");
        var cmd = new SqlCommand(sb.ToString(), c);
    }
}
""", [(5, None)]),
    ("csharp", "command text with newlines", """\
class R {
    void L(IDbCommand cmd) {
        string sSQL;
        sSQL = "select ID " + ControlChars.CrLf;
        sSQL += "  from ACCOUNTS" + Environment.NewLine;
        cmd.CommandText = sSQL;
    }
}
""", [(6, None)]),
    ("java", "concat and builder", """\
class R {
    void load(boolean activeOnly) {
        String sql = "SELECT id, total ";
        sql = sql + "FROM orders";
        if (activeOnly) sql += " WHERE active = 1";
        jdbcTemplate.query(sql, mapper);
    }
    void builder() {
        StringBuilder sb = new StringBuilder(256);
        sb.append("SELECT id ");
        sb.append("FROM orders").append(" WHERE active = 1");
        String sql = sb.toString();
        jdbcTemplate.query(sql, mapper);
        jdbcTemplate.query(sb.toString(), mapper);
    }
}
""", [(6, "query"), (13, "query"), (14, "query")]),
    ("php", "dot-equals", """\
<?php
class R {
    public function load($db, $activeOnly) {
        $sql = "SELECT id, total ";
        $sql .= "FROM orders";
        if ($activeOnly) { $sql .= " WHERE active = 1"; }
        $rows = $db->query($sql);
    }
}
""", [(7, "query")]),
    ("php", "procedural and eol", """\
<?php
function a($conn) {
    $sql = "SELECT a ";
    $sql .= "FROM t";
    $r = mysqli_query($conn, $sql);
}
function b($wpdb) {
    $sql = "SELECT a " . PHP_EOL;
    $sql .= "FROM t" . PHP_EOL;
    $rows = $wpdb->get_results($sql);
}
""", [(5, "mysqli_query"), (10, "get_results")]),
    ("vb", "amp-equals", """\
Public Class R
    Public Sub Load(conn As SqlConnection, activeOnly As Boolean)
        Dim sql As String = "SELECT id, total "
        sql &= "FROM orders"
        If activeOnly Then sql &= " WHERE active = 1"
        Dim cmd2 = New SqlCommand(sql, conn)
    End Sub
    Public Sub Plus(conn As SqlConnection)
        Dim sql = "SELECT id "
        sql = sql & "FROM orders"
        sql += " WHERE x = 1"
        conn.Execute(sql)
    End Sub
    Sub NewLines(cmd As SqlCommand)
        Dim sSQL As String
        sSQL = "select ID " & vbCrLf
        sSQL &= "  from ACCOUNTS" & vbCrLf
        cmd.CommandText = sSQL
    End Sub
End Class
""", [(6, None), (12, "Execute"), (18, None)]),
    ("typescript", "plus-equals", """\
export async function load(db: any, activeOnly: boolean) {
  let sql = "SELECT id, total ";
  sql += "FROM orders";
  if (activeOnly) sql += " WHERE active = 1";
  const rows = await db.query(sql);
}
""", [(5, "query")]),
    ("javascript", "template without substitution", """\
function all(db) {
  let sql = `SELECT a `;
  sql += `FROM t`;
  db.all(sql);
}
""", [(4, "all")]),
]


@pytest.mark.parametrize(("lang", "name", "src", "expected"), _BUILT,
                         ids=[f"{c[0]}-{c[1]}" for c in _BUILT])
def test_built_sql_is_one_query_at_its_use(lang, name, src, expected, tmp_path) -> None:
    assert _queries(lang, src, tmp_path) == expected


def test_single_string_query_is_unchanged(tmp_path) -> None:
    src = """\
class R {
    void LoadOne(SqlConnection conn) {
        var cmd = new SqlCommand("SELECT id FROM orders WHERE id = @id", conn);
    }
}
"""
    assert _queries("csharp", src, tmp_path) == [(3, None)]


# ── never a guess: a value from outside the body is never joined ────────────────────────
_NOT_JOINED = [
    ("python", "parameter append", """\
def f(cur, where):
    sql = "SELECT id "
    sql += "FROM orders "
    sql += where
    cur.execute(sql)
"""),
    ("python", "parameter reassigned", """\
def f(cur, sql=None):
    if not sql:
        sql = "SELECT a "
        sql += "FROM t"
    cur.execute(sql)
"""),
    ("python", "call result", """\
def f(cur):
    sql = "SELECT a "
    sql += "FROM t"
    sql = cur.fetch()
    cur.execute(sql)
"""),
    ("python", "non-literal piece", """\
def f(cur, id):
    sql = "SELECT a "
    sql += "FROM t "
    sql += "WHERE id = " + str(id)
    cur.execute(sql)
"""),
    ("python", "f-string piece", """\
def f(cur, uid):
    sql = "SELECT id "
    sql += "FROM orders "
    sql += f"WHERE uid = {uid}"
    cur.execute(sql)
"""),
    ("python", "other compound op", """\
def f(cur):
    sql = "SELECT a "
    sql += "FROM t"
    sql *= 2
    cur.execute(sql)
"""),
    ("python", "loop target", """\
def f(cur, items):
    sql = "SELECT a "
    sql += "FROM t"
    for sql in items:
        pass
    cur.execute(sql)
"""),
    ("python", "tuple target", """\
def f(cur):
    sql = "SELECT a "
    sql += "FROM t"
    sql, p = build()
    cur.execute(sql)
"""),
    ("python", "walrus", """\
def f(cur):
    sql = "SELECT a "
    sql += "FROM t"
    if (sql := get()):
        pass
    cur.execute(sql)
"""),
    ("python", "with as", """\
def f(cur):
    sql = "SELECT a "
    sql += "FROM t"
    with open("x") as sql:
        pass
    cur.execute(sql)
"""),
    ("python", "nested function", """\
def f(cur):
    sql = "SELECT a "
    sql += "FROM t"
    def g():
        nonlocal sql
        sql = other()
    cur.execute(sql)
"""),
    ("python", "case pattern", """\
def f(cur, v):
    sql = "SELECT a "
    sql += "FROM t"
    match v:
        case [sql]:
            pass
    cur.execute(sql)
"""),
    ("csharp", "ref argument", """\
class R {
    void F(SqlConnection conn) {
        var sql = "SELECT a ";
        sql += "FROM t";
        Fix(ref sql);
        conn.Query(sql);
    }
}
"""),
    ("csharp", "lambda write", """\
class R {
    void F(SqlConnection c) {
        var sql = "SELECT a ";
        sql += "FROM t";
        Action a = () => { sql = Other(); };
        c.Execute(sql);
    }
}
"""),
    ("csharp", "tuple deconstruction", """\
class R {
    void F(SqlConnection c, D d) {
        var sql = "SELECT a ";
        sql += "FROM t";
        (sql, var x) = d.Get();
        c.Execute(sql);
    }
}
"""),
    ("csharp", "interpolated piece", """\
class R {
    void F(SqlConnection conn, int id) {
        var s = "SELECT a ";
        s += "FROM t ";
        s += $"WHERE id = {id}";
        conn.Query(s);
    }
}
"""),
    ("csharp", "builder to helper", """\
class R {
    void F(SqlConnection c) {
        var sb = new StringBuilder("SELECT a ");
        sb.Append("FROM t");
        AddWhere(sb);
        new SqlCommand(sb.ToString(), c);
    }
}
"""),
    ("csharp", "builder changed", """\
class R {
    void F(SqlConnection c) {
        var sb = new StringBuilder("SELECT a ");
        sb.Append("FROM t");
        sb.Insert(0, x);
        new SqlCommand(sb.ToString(), c);
    }
}
"""),
    ("csharp", "chained append of a parameter", """\
class R {
    void F(SqlConnection c, string p) {
        var sb = new StringBuilder("SELECT a ");
        sb.Append("FROM t ");
        sb.Append("WHERE ").Append(p);
        new SqlCommand(sb.ToString(), c);
    }
}
"""),
    ("csharp", "builder reassigned", """\
class R {
    void F(SqlConnection c) {
        var sb = new StringBuilder("SELECT a ");
        sb.Append("FROM t");
        sb = "x";
        new SqlCommand(sb.ToString(), c);
    }
}
"""),
    ("java", "inline append of a parameter", """\
class R {
    void f(String p) {
        StringBuilder sb = new StringBuilder("SELECT a ");
        sb.append("FROM t ");
        log(sb.append(p));
        jdbcTemplate.query(sb.toString(), m);
    }
}
"""),
    ("java", "append of a parameter", """\
class R {
    void f(String p) {
        StringBuilder sb = new StringBuilder("SELECT a ");
        sb.append("FROM t ");
        sb.append(p);
        jdbcTemplate.query(sb.toString(), m);
    }
}
"""),
    ("php", "by-reference helper", """\
<?php
function f($db) {
    $sql = "SELECT a ";
    $sql .= "FROM t";
    addWhere($sql);
    $db->query($sql);
}
"""),
    ("php", "foreach target", """\
<?php
function f($db, $xs) {
    $sql = "SELECT a ";
    $sql .= "FROM t";
    foreach ($xs as $sql) { }
    $db->query($sql);
}
"""),
    ("php", "list target", """\
<?php
function f($db) {
    $sql = "SELECT a ";
    $sql .= "FROM t";
    [$sql, $p] = build();
    $db->query($sql);
}
"""),
    ("php", "extract", """\
<?php
function f($db, $a) {
    $sql = "SELECT a ";
    $sql .= "FROM t";
    extract($a);
    $db->query($sql);
}
"""),
    ("php", "reference alias", """\
<?php
function f($db) {
    $sql = "SELECT a ";
    $ref = &$sql;
    $ref = "hello ";
    $sql .= "FROM t";
    $db->query($sql);
}
"""),
    ("php", "reference in an array", """\
<?php
function f($db) {
    $sql = "SELECT a ";
    $sql .= "FROM t";
    $refs = [&$sql];
    $db->query($sql);
}
"""),
    ("php", "interpolated piece", """\
<?php
function f($db, $id) { $s = "SELECT a "; $s .= "FROM t "; $s .= "WHERE id = $id"; $db->query($s); }
"""),
    ("vb", "case variant", """\
Public Class R
    Sub F(con As SqlConnection)
        Dim sql = "SELECT a "
        sql &= "FROM t"
        Sql = GetQuery()
        Dim cmd = New SqlCommand(sql, con)
    End Sub
End Class
"""),
    ("vb", "byref helper", """\
Public Class R
    Sub F(con As SqlConnection)
        Dim sql = "SELECT a "
        sql &= "FROM t"
        AddWhere(sql)
        con.Execute(sql)
    End Sub
End Class
"""),
    ("vb", "conditional call result", """\
Public Class R
    Sub F(con As SqlConnection, x As Boolean)
        Dim sql = "SELECT a "
        sql &= "FROM t"
        If x Then sql = GetQuery()
        con.Execute(sql)
    End Sub
End Class
"""),
    ("typescript", "optional parameter", """\
export function f(db: any, q?: string) {
  if (!q) { q = "SELECT a "; q += "FROM t"; }
  db.query(q);
}
"""),
    ("typescript", "destructuring", """\
export function f(db: any, o: any) {
  let sql = "SELECT a ";
  sql += "FROM t";
  ({ sql } = o);
  db.query(sql);
}
"""),
    ("typescript", "renamed destructuring", """\
export function f(db: any, o: any) {
  let sql = "SELECT a ";
  sql += "FROM t";
  ({ q: sql } = o);
  db.query(sql);
}
"""),
    ("typescript", "callback write", """\
export function f(db: any, api: any) {
  let sql = "SELECT a ";
  sql += "FROM t";
  api.get().then((r: any) => { sql = r.text; });
  db.query(sql);
}
"""),
    ("typescript", "block redeclaration", """\
export function f(db: any, x: boolean) {
  let sql = "SELECT id ";
  if (x) { let sql = "SELECT x "; sql += "FROM y"; console.log(sql); }
  db.query(sql);
}
"""),
    ("typescript", "template substitution", """\
export function f(db: any, id: number) {
  let s = `SELECT a `; s += `FROM t `; s += `WHERE id = ${id}`; db.query(s);
}
"""),
]


@pytest.mark.parametrize(("lang", "name", "src"), _NOT_JOINED,
                         ids=[f"{c[0]}-{c[1]}" for c in _NOT_JOINED])
def test_outside_value_is_never_joined(lang, name, src, tmp_path) -> None:
    assert _queries(lang, src, tmp_path) == []


# ── not a query: non-SQL text, non-data-access calls, a query already recorded ──────────
def test_labels_and_text_sinks_are_not_queries(tmp_path) -> None:
    py = """\
def labels(log):
    msg = "Select a file "
    msg += "from the list"
    print(msg)
    log.debug("running %s", msg)

def html(resp):
    h = "<option value='update'>Update</option>"
    h += "<div>Set preferences</div>"
    resp.write(h)
"""
    cs = """\
class R {
    void Show() {
        var msg = "Select a file ";
        msg += "from the list";
        MessageBox.Show(msg);
        Console.WriteLine(msg);
    }
}
"""
    assert _queries("python", py, tmp_path) == []
    assert _queries("csharp", cs, tmp_path) == []


def test_only_the_first_argument_is_the_sql(tmp_path) -> None:
    src = """\
def f(cur, log):
    sql = "SELECT a "
    sql += "FROM t"
    log.debug("running %s", sql)
    cur.execute(sql)
"""
    assert _queries("python", src, tmp_path) == [(5, "execute")]


def test_an_http_call_keeps_its_classification(tmp_path) -> None:
    src = """\
import requests
def f():
    sql = "SELECT a "
    sql += "FROM t"
    requests.get(sql)
"""
    assert _queries("python", src, tmp_path) == []


@pytest.mark.parametrize("src", [
    # the declaration already holds SQL: it is the record, the use adds none
    'def f(cur):\n    sql = "SELECT a FROM t"\n    sql += " WHERE b = 1"\n    cur.execute(sql)\n',
    # a later assignment or append already holds SQL
    'def f(cur):\n    sql = ""\n    sql = "SELECT id FROM orders WHERE x = 1"\n    cur.execute(sql)\n',
    'def f(cur):\n    sql = ""\n    sql += "SELECT id FROM orders WHERE x = 1"\n    cur.execute(sql)\n',
])
def test_a_query_already_recorded_is_not_recorded_again(src, tmp_path) -> None:
    lines = [line for line, _ in _queries("python", src, tmp_path)]
    assert len(lines) == 1 and lines[0] < 4  # the assignment, not cur.execute (line 4)


def test_a_builder_already_holding_sql_is_not_recorded_again(tmp_path) -> None:
    src = """\
class R {
    void f() {
        StringBuilder sb = new StringBuilder("SELECT a FROM t");
        sb.append(" WHERE b = 1");
        String sql = sb.toString();
        jdbcTemplate.query(sql, m);
    }
}
"""
    assert [line for line, _ in _queries("java", src, tmp_path)] == [3]


def test_a_use_before_the_appends_is_not_joined(tmp_path) -> None:
    src = """\
def f(cur, n):
    sql = "SELECT id "
    while n:
        cur.execute(sql)
        sql += "FROM orders"
"""
    assert _queries("python", src, tmp_path) == []


@pytest.mark.parametrize(("lang", "src"), [
    # generic names on a receiver that is not a database handle: an LLM chain, an index,
    # a process, a LINQ query, the DOM
    ("python", 'def f(chain):\n    p = "Select the three best products "\n'
               '    p += "from the catalogue below."\n    chain.run(p)\n'),
    ("python", 'def f(index):\n    q = "Select the answer "\n    q += "from the context"\n'
               '    index.query(q)\n'),
    ("python", 'import subprocess\ndef f():\n    cmd = "delete "\n    cmd += "from /tmp/x"\n'
               '    subprocess.run(cmd)\n'),
    ("csharp", 'class R {\n  void F(G g) {\n    var m = "Select a customer ";\n'
               '    m += "from the list";\n    g.Select(m);\n  }\n}\n'),
    ("typescript", 'export function f() {\n  let s = "SELECT a ";\n  s += "FROM t";\n'
                   '  document.querySelectorAll(s);\n}\n'),
])
def test_generic_names_count_only_on_a_database_handle(lang, src, tmp_path) -> None:
    assert _queries(lang, src, tmp_path) == []


@pytest.mark.parametrize(("lang", "src", "expected"), [
    ("php", '<?php\nclass M {\n  function f() {\n    $sql = "SELECT a ";\n    $sql .= "FROM t";\n'
            '    $this->db->query($sql);\n  }\n}\n', [(6, "query")]),
    ("php", '<?php\nfunction f() {\n  $sql = "SELECT a ";\n  $sql .= "FROM t";\n'
            '  DB::select($sql);\n}\n', [(5, "select")]),
    ("typescript", 'export async function f(pool: any) {\n  let q = "SELECT a ";\n  q += "FROM t";\n'
                   '  await pool.query(q);\n}\n', [(4, "query")]),
])
def test_generic_names_on_a_database_handle_count(lang, src, expected, tmp_path) -> None:
    assert _queries(lang, src, tmp_path) == expected


def test_sql_built_from_an_earlier_local_counts(tmp_path) -> None:
    src = """\
def f(cur):
    base = "SELECT a "
    sql = base + "FROM t"
    cur.execute(sql)
"""
    assert _queries("python", src, tmp_path) == [(4, "execute")]


def test_built_cte_counts(tmp_path) -> None:
    src = """\
def f(cur):
    sql = "WITH x AS (SELECT a "
    sql += "FROM t) SELECT a "
    sql += "FROM x"
    cur.execute(sql)
"""
    assert _queries("python", src, tmp_path) == [(5, "execute")]


# ── built_queries: the gate, the limits and the fail-safe ───────────────────────────────
def _body(src: str):
    from breezeai_cog.parsers.treesitter import parse_source

    tree = parse_source("python", src.encode())
    return tree.root_node.named_children[0].child_by_field_name("body")


def test_body_without_quoted_sql_is_skipped() -> None:
    from breezeai_cog.parsers.sql_variables import built_queries

    src = 'def f(cur):\n    x = "hello "\n    x += "world"\n    cur.execute(x)\n'
    assert built_queries(_body(src), src.encode(), "python") is None
    assert built_queries(None, b"", "python") is None


def test_built_queries_never_raises(monkeypatch) -> None:
    from breezeai_cog.parsers import sql_variables

    def boom(self, body):
        raise RuntimeError("unexpected tree")

    monkeypatch.setattr(sql_variables._Pass, "run", boom)
    src = 'def f(cur):\n    sql = "SELECT a "\n    sql += "FROM t"\n    cur.execute(sql)\n'
    assert sql_variables.built_queries(_body(src), src.encode(), "python") is None


@pytest.mark.parametrize("appends", [
    ['    sql += " AND a = 1"\n'] * 201,  # too many pieces
    ['    sql += "' + "x" * 70_000 + '"\n'],  # too long
])
def test_oversized_local_is_left_out(appends) -> None:
    from breezeai_cog.parsers.sql_variables import built_queries

    src = ('def f(cur):\n    sql = "SELECT a "\n    sql += "FROM t"\n' + "".join(appends)
           + "    cur.execute(sql)\n")
    assert built_queries(_body(src), src.encode(), "python") is None


def test_a_left_out_local_stops_growing() -> None:
    from breezeai_cog.parsers import sql_variables

    src = ('def f(cur):\n    sql = "INSERT INTO t VALUES "\n'
           + '    sql += "(1),"\n' * 5000 + "    cur.execute(sql)\n")
    body = _body(src)
    run = sql_variables._Pass(src.encode(), "python")
    assert run.run(body) is None
    assert "sql" in run.out and run.locals["sql"].parts == []


def test_joined_text_is_handed_to_the_call() -> None:
    from breezeai_cog.parsers.sql_variables import built_queries

    src = 'def f(cur):\n    sql = "SELECT a "\n    sql += "FROM t"\n    cur.execute(sql)\n'
    built = built_queries(_body(src), src.encode(), "python")
    assert built is not None and list(built.calls.values()) == ["SELECT a  FROM t"]
    assert built.direct == {}


# ── the shared helpers ──────────────────────────────────────────────────────────────────
def test_built_string_has_query_needs_sql_at_the_start() -> None:
    assert built_string_has_query(["SELECT a ", "FROM t"])
    assert built_string_has_query(["select ID ", " ", "  from ACCOUNTS"])
    assert built_string_has_query(["WITH x AS (SELECT a FROM t) ", "SELECT a FROM x"])
    assert not built_string_has_query(["<option value='update'>Update</option>", "Set x"])
    assert not built_string_has_query(["Hello ", "SELECT a FROM t"])
    assert not built_string_has_query(["Select"])


def test_is_sql_sink() -> None:
    for name in ("execute", "executemany", "ExecuteNonQuery", "prepare", "read_sql",
                 "get_results", "mysqli_query", "FromSqlRaw"):
        assert is_sql_sink(name), name
    for name in ("print", "debug", "Show", "write", "get", "update", "WriteLine", "query",
                 "run", "all", "exec", "select"):
        assert not is_sql_sink(name), name


def test_generic_sink_needs_a_database_handle() -> None:
    for callee, name in (("db.query", "query"), ("$this->db->query", "query"),
                         ("conn.QueryAsync<int>", "QueryAsync"), ("cursor.query", "query"),
                         ("db.run", "run"), ("db.all", "all"), ("DB::select", "select"),
                         ("pool.query", "query"), ("jdbcTemplate.query", "query"),
                         ("self.connection.exec", "exec")):
        assert is_sql_sink(name, callee), callee
    for callee, name in (("chain.run", "run"), ("index.query", "query"),
                         ("subprocess.run", "run"), ("g.Select", "Select"),
                         ("document.querySelectorAll", "querySelectorAll"),
                         ("db.querySelector", "querySelector"), ("query", "query"),
                         ("child_process.exec", "exec")):
        assert not is_sql_sink(name, callee), callee


def test_is_sql_command_type() -> None:
    for name in ("SqlCommand", "OleDbCommand", "MySqlCommand", "NpgsqlCommand",
                 "SqlDataAdapter", "System.Data.SqlClient.SqlCommand"):
        assert is_sql_command_type(name), name
    for name in ("Command", "RelayCommand", "SqlConnection", "StringBuilder"):
        assert not is_sql_command_type(name), name


def test_sql_argument() -> None:
    assert sql_argument("execute", 2) == 0
    assert sql_argument("mysqli_query", 2) == 1
    assert sql_argument("pg_query", 1) == 0  # the connection left out
    assert sql_argument("pg_prepare", 3) == 2
