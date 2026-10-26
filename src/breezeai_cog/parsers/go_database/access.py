"""Database access in Go: ``db_method_call`` on calls whose receiver is bound to a database
library type — never on a method name alone (``client.Get`` and ``cache.Save`` stay plain)::

    db, _ := sql.Open("postgres", dsn)
    rows, err := db.QueryContext(ctx, q)         → db_method_call QueryContext, hint sql
    db.Get(&u, "SELECT …", id)                   → sqlx                  (db *sqlx.DB)
    db.Where("email = ?", e).First(&u)           → First, hint gorm      (one record per chain)
    pool.QueryRow(ctx, q, id).Scan(&n)           → QueryRow, hint pgx
    coll := client.Database("app").Collection("users")
    coll.FindOne(ctx, filter)                    → FindOne, hint mongodb
    rdb.Set(ctx, "k", v, 0)                      → Set, hint redis

The detector enriches the base parser's record of the statement or call (overriding a
``query_statement`` / misread tag); a control-flow header keeps no semantic type, so a call in
``if err := db.QueryRow(…); …`` gets a record of its own, as the base parser does for one."""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ...emit import statement_id
from ..go.declarations import declarations
from ..go.imports import GoIndex
from ..go.routing import file_analyzer
from ..go.values import ValueSpec, typed_calls

_SQL_ACCESS = frozenset({"Query", "QueryContext", "QueryRow", "QueryRowContext", "Exec", "ExecContext"})
_SQLX_ACCESS = _SQL_ACCESS | frozenset({
    "Get", "GetContext", "Select", "SelectContext", "NamedExec", "NamedExecContext", "NamedQuery",
    "NamedQueryContext", "Queryx", "QueryxContext", "QueryRowx", "QueryRowxContext", "MustExec",
    "MustExecContext"})
_GORM_CHAIN = frozenset({
    "Where", "Not", "Or", "Model", "Table", "Select", "Omit", "Order", "Limit", "Offset", "Joins",
    "InnerJoins", "Preload", "Group", "Having", "Distinct", "Scopes", "Session", "WithContext",
    "Debug", "Unscoped", "Clauses", "Attrs", "Assign", "Begin", "Raw"})
_GORM_ACCESS = frozenset({
    "Find", "FindInBatches", "First", "Last", "Take", "Scan", "Pluck", "Count", "Create",
    "CreateInBatches", "Save", "Update", "Updates", "UpdateColumn", "UpdateColumns", "Delete",
    "Exec", "Row", "Rows", "FirstOrCreate", "FirstOrInit"})
_PGX_ACCESS = frozenset({"Query", "QueryRow", "Exec", "SendBatch", "CopyFrom"})
_MONGO_ACCESS = frozenset({
    "Find", "FindOne", "InsertOne", "InsertMany", "UpdateOne", "UpdateMany", "UpdateByID",
    "DeleteOne", "DeleteMany", "ReplaceOne", "Aggregate", "CountDocuments",
    "EstimatedDocumentCount", "Distinct", "FindOneAndUpdate", "FindOneAndDelete",
    "FindOneAndReplace", "BulkWrite", "Watch"})
#: go-redis methods that are not commands.
_REDIS_NOT_COMMANDS = frozenset({
    "Close", "Options", "Pipeline", "TxPipeline", "Pipelined", "TxPipelined", "WithContext",
    "Context", "AddHook", "PoolStats", "Conn", "String", "Subscribe", "PSubscribe", "SSubscribe"})

#: (spec, hint, methods that access the database; None = every method but _REDIS_NOT_COMMANDS)
_LIBRARIES: list[tuple[ValueSpec, str, frozenset[str] | None]] = [
    (ValueSpec("database/sql", frozenset({"DB", "Tx", "Conn", "Stmt"}),
               constructors={"Open": "DB", "OpenDB": "DB"},
               derived={("DB", "Begin"): "Tx", ("DB", "BeginTx"): "Tx", ("DB", "Conn"): "Conn",
                        ("DB", "Prepare"): "Stmt", ("DB", "PrepareContext"): "Stmt",
                        ("Tx", "Prepare"): "Stmt", ("Tx", "PrepareContext"): "Stmt",
                        ("Conn", "BeginTx"): "Tx"}),
     "sql", _SQL_ACCESS),
    (ValueSpec("github.com/jmoiron/sqlx", frozenset({"DB", "Tx", "Stmt", "NamedStmt"}),
               constructors={"Open": "DB", "Connect": "DB", "ConnectContext": "DB",
                             "MustConnect": "DB", "MustOpen": "DB", "NewDb": "DB"},
               derived={("DB", "Beginx"): "Tx", ("DB", "BeginTxx"): "Tx", ("DB", "MustBegin"): "Tx",
                        ("DB", "Preparex"): "Stmt", ("DB", "PrepareNamed"): "NamedStmt",
                        ("Tx", "Preparex"): "Stmt"}),
     "sqlx", _SQLX_ACCESS),
    (ValueSpec("gorm.io/gorm", frozenset({"DB"}), constructors={"Open": "DB"},
               derived={("DB", m): "DB" for m in _GORM_CHAIN}),
     "gorm", _GORM_ACCESS),
    (ValueSpec("github.com/jinzhu/gorm", frozenset({"DB"}), constructors={"Open": "DB"},
               derived={("DB", m): "DB" for m in _GORM_CHAIN}),
     "gorm", _GORM_ACCESS),
    (ValueSpec("github.com/jackc/pgx", frozenset({"Conn", "Tx"}),
               constructors={"Connect": "Conn", "ConnectConfig": "Conn"},
               derived={("Conn", "Begin"): "Tx", ("Conn", "BeginTx"): "Tx"}),
     "pgx", _PGX_ACCESS),
    (ValueSpec("github.com/jackc/pgx/v5/pgxpool", frozenset({"Pool", "Conn", "Tx"}),
               constructors={"New": "Pool", "NewWithConfig": "Pool", "Connect": "Pool"},
               derived={("Pool", "Begin"): "Tx", ("Pool", "BeginTx"): "Tx", ("Pool", "Acquire"): "Conn"}),
     "pgx", _PGX_ACCESS),
    (ValueSpec("github.com/jackc/pgx/v4/pgxpool", frozenset({"Pool", "Conn", "Tx"}),
               constructors={"Connect": "Pool", "ConnectConfig": "Pool"},
               derived={("Pool", "Begin"): "Tx", ("Pool", "BeginTx"): "Tx", ("Pool", "Acquire"): "Conn"}),
     "pgx", _PGX_ACCESS),
    *[(ValueSpec(path, frozenset({"Client", "Database", "Collection"}),
                 constructors={"Connect": "Client", "NewClient": "Client"},
                 derived={("Client", "Database"): "Database", ("Database", "Collection"): "Collection"}),
       "mongodb", _MONGO_ACCESS)
      for path in ("go.mongodb.org/mongo-driver/mongo", "go.mongodb.org/mongo-driver/v2/mongo")],
    *[(ValueSpec(path, frozenset({"Client", "ClusterClient", "UniversalClient", "Ring", "Tx"}),
                 constructors={"NewClient": "Client", "NewClusterClient": "ClusterClient",
                               "NewUniversalClient": "UniversalClient", "NewFailoverClient": "Client",
                               "NewRing": "Ring"}),
       "redis", None)
      for path in ("github.com/redis/go-redis", "github.com/go-redis/redis")],
]

#: The detector's own byte guard: one marker per library.
_MARKERS = (b'"database/sql"', b"jmoiron/sqlx", b"gorm", b"jackc/pgx", b"mongo-driver", b"redis")


_returning_cache: tuple[int, dict[bytes, frozenset[str]]] | None = None


def _returning(index: GoIndex | None) -> dict[bytes, frozenset[str]]:
    """Repo functions declared to return a database type: name → the libraries' import paths.
    Built once per index (one slot: a worker parses one repo)."""
    global _returning_cache
    if index is None:
        return {}
    if _returning_cache is None or _returning_cache[0] != id(index):
        found: dict[bytes, set[str]] = {}
        for (_, name), rtype in declarations(index).results.items():
            if isinstance(rtype, tuple) and any(spec.owns(rtype[0]) and rtype[1] in spec.types
                                                for spec, _, _ in _LIBRARIES):
                found.setdefault(name.encode() + b"(", set()).add(rtype[0])
        _returning_cache = (id(index), {k: frozenset(v) for k, v in found.items()})
    return _returning_cache[1]


def detect_access(dc: DetectContext) -> Detection | None:
    index = dc.ctx.resolution_index if isinstance(dc.ctx.resolution_index, GoIndex) else None
    via_functions = {ip for name, ips in _returning(index).items() if name in dc.source for ip in ips}
    if not via_functions and not any(m in dc.source for m in _MARKERS):
        return None
    a = file_analyzer(dc.root, dc.source, dc.path, index)
    own_calls = {s.id for s in dc.record.statements if s.nodeType == "call_expression"}
    found: list[Emit] = []
    for spec, hint, access in _LIBRARIES:
        for tc in typed_calls(a, spec, returned=any(spec.owns(ip) for ip in via_functions)):
            if access is None:
                if tc.method in _REDIS_NOT_COMMANDS:
                    continue
            elif tc.method not in access:
                continue
            if (tc.value.type, tc.method) in spec.derived:
                continue  # a chain step (gorm Where/Model): the terminal call is the access
            line, col = tc.call.start_point[0] + 1, tc.call.start_point[1]
            same = statement_id(dc.path, line, col) in own_calls
            found.append(Emit(
                tc.call,
                {"semanticType": "db_method_call", "method": tc.method, "dataAccessHint": hint},
                enrich="same" if same else "own",
                reclassify=frozenset({"query_statement", "db_method_call", "api_call"}),
            ))
    return Detection(found) if found else None


register_detector(Detector(
    name="go-database", gate=Gate("go", order=110), detect=detect_access,
    frameworks=(),
))
