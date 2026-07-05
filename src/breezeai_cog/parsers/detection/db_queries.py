"""ORM / database method-call detection.

Returns an inferred ``dataAccessHint`` (the DB/ORM) for a normalized call, else
``None``. Detection is heuristic (``dataAccessHint`` is documented as inferred):
distinctive method names map directly; ambiguous ORM verbs (``findOne``/``save``…)
are refined by receiver hints.

Mostly language-agnostic, with one language-aware gate: an optional ``language`` tag
suppresses Entity-Framework verbs in known non-.NET files (see ``_DOTNET``) so that,
e.g., ``.include()`` in TypeScript is not mislabeled as EF. High-collision generic
verbs (``find``/``create``/…) require a positive DB receiver rather than defaulting to
``orm`` — this is what keeps ``Array.find`` / ``dict.update`` / ``Cookies.remove`` out.

The distinctive table mirrors the legacy ``DB_METHOD_MAP`` (JS ``utils.js``) — one entry
per DB/ORM, most-specific DB listed first so it wins a name collision. Raw-SQL builders
(``executeQuery``/``prepareStatement``/``$queryRaw``…) live in :mod:`queries` and are
classified as ``query_statement`` before this runs.
"""

from __future__ import annotations

# DB/ORM -> distinctive method names (lowercased). Order matters: on a name collision
# the first DB wins (mirrors the legacy reverse-map "first one wins").
_DB_METHODS: dict[str, tuple[str, ...]] = {
    "sql": ("executemany",),
    "sequelize": ("findall", "findbypk", "bulkcreate", "findorcreate", "findandcountall", "upsert"),
    "prisma": ("findfirst", "findunique", "findmany"),
    "typeorm": ("createquerybuilder", "getrepository"),
    "mongodb": (
        "aggregate", "insertone", "insertmany", "updateone", "updatemany",
        "deleteone", "deletemany", "replaceone", "bulkwrite",
        "findoneandupdate", "findbyidandupdate", "findoneanddelete", "findoneandreplace",
        "countdocuments", "estimateddocumentcount",
    ),
    "neo4j": ("readtransaction", "writetransaction", "executeread", "executewrite"),
    "couchdb": ("alldocs", "bulkdocs", "getindexes"),
    "redis": (
        "hget", "hset", "hgetall", "hdel", "hmset", "hmget",
        "lpush", "rpush", "lpop", "rpop", "lrange",
        "sadd", "srem", "smembers", "sismember",
        "zadd", "zrem", "zrange", "zrangebyscore",
        "mset", "mget",
    ),
    "dynamodb": (
        "putitem", "deleteitem", "updateitem",
        "batchgetitem", "batchwriteitem", "transactgetitems", "transactwriteitems",
    ),
    "elasticsearch": ("msearch",),
    "firebase": ("getdocs", "getdoc", "setdoc", "updatedoc", "deletedoc", "adddoc", "onsnapshot"),
    "entity_framework": (
        "tolistasync", "tolist", "toarrayasync", "toarray",
        "firstordefaultasync", "firstordefault", "firstasync",
        "singleordefaultasync", "singleordefault", "singleasync",
        "lastordefaultasync", "lastordefault",
        "countasync", "longcountasync", "anyasync", "allasync",
        "minasync", "maxasync", "sumasync", "averageasync",
        "findasync", "addasync", "addrangeasync", "savechangesasync", "savechanges",
        "include", "theninclude", "asnotracking", "astracking",
        "fromsqlraw", "fromsqlinterpolated", "executesqlraw", "executesqlinterpolated",
    ),
    "django": (
        "select_related", "prefetch_related",
        "get_or_create", "update_or_create", "bulk_update", "values_list",
    ),
    "sqlalchemy": ("filter_by", "session_query", "add_all"),
}

# Reverse lookup: method (lowercased) -> DB; first DB in _DB_METHODS wins a collision.
_DISTINCTIVE: dict[str, str] = {}
for _db, _methods in _DB_METHODS.items():
    for _m in _methods:
        _DISTINCTIVE.setdefault(_m, _db)

# Ambiguous ORM verbs -> generic hint, refined by receiver below.
_GENERIC = {
    "findone", "findbyid", "find", "save", "create", "update", "delete", "remove",
    "persist", "merge", "query", "execute",
}

# Receiver substring -> hint (refines _GENERIC).
_RECEIVER_HINTS = (
    ("prisma", "prisma"), ("sequelize", "sequelize"), ("mongoose", "mongodb"),
    ("repository", "typeorm"), ("repo", "typeorm"), ("entitymanager", "typeorm"),
    ("session", "sqlalchemy"), ("queryset", "django"), ("objects", "django"),
)

# Receiver terminal-segment suffixes that are clearly NOT a database: a generic ORM verb
# (``save``/``delete``/``create``…) on one of these is app logic, not data access, so we
# drop the weak ``"orm"`` fallback for them. Matched on the receiver's final segment by
# suffix (``formState`` -> ``state``, ``userCache`` -> ``cache``) — deliberately narrow to
# avoid suppressing real ORM saves (``user.save``, ``userStore.save`` are NOT listed). A
# distinctive method or a positive receiver hint (checked first) always overrides this.
_NON_DB_RECEIVERS = (
    "cache", "logger", "emitter", "eventbus", "eventemitter", "state",
    "buffer", "console", "clipboard", "factory", "list", "items", "queue", "stack",
)

# Django queryset verbs (ambiguous alone -> require a queryset-ish receiver).
_DJANGO_VERBS = {"filter", "get", "all", "exclude", "annotate", "values"}

# Neo4j runs Cypher via ``session.run(...)`` / ``tx.run(...)``. ``run`` is too generic to
# list as a distinctive method, so require a driver/session/transaction-ish receiver.
_NEO4J_RUN_RECEIVERS = ("session", "tx", "transaction", "driver", "neo4j")

# Languages whose files may legitimately use Entity Framework verbs. EF method names
# (``ToListAsync``/``Include``/…) collide with unrelated code in other stacks — most
# painfully ``.include()``, which is TypeORM/array/RxJS in JS but EF in C#. When we KNOW
# the file is a non-.NET language we treat the EF match as a collision and fall through;
# an unknown language (``None``) stays permissive for backward compatibility.
_DOTNET = frozenset({"csharp", "vb"})

# Generic verbs that collide heavily with ordinary (non-DB) code: ``find`` is
# ``Array.prototype.find``, ``update`` is ``dict.update``/``Map.set``-adjacent, ``create``
# is ``Object.create``/Zustand ``create``, ``remove`` is ``Cookies.remove``, etc. For these
# we require *positive* evidence of a DB receiver (opt-in) instead of defaulting to ``orm``
# (opt-out) — mirroring the api-call detector, which only fires on a client hint. Real ORM
# calls on these verbs still match earlier via ``_RECEIVER_HINTS`` (repository/session/…).
_HIGH_COLLISION = frozenset({"find", "create", "update", "delete", "remove", "persist", "merge"})

# Terminal receiver-segment suffixes that positively signal a DB/ORM handle (beyond the
# vendor substrings in _RECEIVER_HINTS). Matched by suffix on the receiver's last segment
# (``userModel`` -> ``model``, ``orderDao`` -> ``dao``). Kept unambiguous — no short tokens
# like ``em``/``db`` that would also match ``item``/``webdb``.
_DB_RECEIVER_SUFFIXES = (
    "repository", "repo", "model", "models", "dao", "collection",
    "datasource", "queryrunner", "database", "manager",
)


def match_db(callee: str, method: str, language: str | None = None) -> str | None:
    m = method.lower()
    low = callee.lower()
    if m in _DISTINCTIVE:
        db = _DISTINCTIVE[m]
        # EF verbs are .NET-only; suppress them in a known non-.NET file (name collision).
        if not (db == "entity_framework" and language is not None and language not in _DOTNET):
            return db
    if m in _GENERIC:
        for needle, hint in _RECEIVER_HINTS:  # positive vendor hint wins
            if needle in low:
                return hint
        receiver = low.rsplit(".", 1)[0].rsplit(".", 1)[-1] if "." in low else ""
        if m in _HIGH_COLLISION:
            # Opt-in: data access only on a positive DB receiver, else drop (no bare `orm`).
            if receiver and any(receiver.endswith(s) for s in _DB_RECEIVER_SUFFIXES):
                return "orm"
            return None
        if receiver and any(receiver.endswith(s) for s in _NON_DB_RECEIVERS):
            return None  # a cache/collection/UI-state/etc. receiver — not data access
        return "orm"
    if m in _DJANGO_VERBS and ("objects" in low or "queryset" in low):
        return "django"
    if m == "run" and "." in low:
        # callee includes the method (``session.run``); the receiver is everything before
        # it, and we match on the receiver's final segment (``this.session`` -> ``session``).
        receiver_last = low.rsplit(".", 1)[0].rsplit(".", 1)[-1]
        if any(receiver_last == r or receiver_last.endswith(r) for r in _NEO4J_RUN_RECEIVERS):
            return "neo4j"
    return None
