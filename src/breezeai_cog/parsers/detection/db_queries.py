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
# ``query``/``execute`` belong here too: in TS/JS they are dominated by non-DB callers —
# Apollo ``client.query``/``api.execute``, Angular animation ``query(':enter')`` and test
# ``DebugElement.query``, and the command/use-case pattern ``useCase.execute`` — so a bare
# ``.query()``/``.execute()`` should NOT default to ``orm``. Genuine raw-SQL callers use a
# recognisable handle (``dataSource``/``queryRunner``/``connection``/``repository``) picked
# up by the suffix/hint gates below.
_HIGH_COLLISION = frozenset(
    {"find", "create", "update", "delete", "remove", "persist", "merge", "query", "execute"}
)

# Terminal receiver-segment suffixes that positively signal a DB/ORM handle (beyond the
# vendor substrings in _RECEIVER_HINTS). Matched by suffix on the receiver's last segment
# (``userModel`` -> ``model``, ``orderDao`` -> ``dao``). Kept unambiguous — no short tokens
# like ``em``/``db`` that would also match ``item``/``webdb``. ``connection``/``conn`` cover
# raw driver handles for ``query``/``execute`` (mysql2 ``connection.execute``, node-pg
# ``conn.query``); ``pool``/``client`` are deliberately excluded — they collide with worker
# pools (``workerPool.execute``) and non-DB clients (Apollo ``client.query``).
_DB_RECEIVER_SUFFIXES = (
    "repository", "repo", "model", "models", "dao", "collection",
    "datasource", "queryrunner", "database", "manager", "connection", "conn",
)

# ElasticSearch / OpenSearch client verbs. These collide with ordinary code (``search`` is
# ``String.prototype.search``; app repos/services expose ``.search()`` too — 270+ in one repo)
# and with HTTP (``get``/``delete``), so they are gated on an ES-*client* receiver and the
# high-collision HTTP verbs (get/update/delete/create) are deliberately excluded. ``msearch``
# stays in _DISTINCTIVE (unambiguous, no receiver needed); ``mget`` is left to Redis.
_ES_VERBS = frozenset({"search", "bulk", "index", "scroll", "reindex", "count"})
# Terminal receiver segment that identifies an ES client (``this.client`` / ``esClient`` /
# ``osClient``). Anything ending in ``client`` plus the explicit ES names — but NOT the DB
# receivers above, so ``xxxCustomRepository.search`` / ``xService.search`` are excluded.
_ES_RECEIVERS = frozenset({"client", "esclient", "es", "elastic", "elasticsearch", "opensearch", "osclient"})


def match_db(callee: str, method: str, language: str | None = None) -> str | None:
    m = method.lower()
    low = callee.lower()
    if m in _DISTINCTIVE:
        db = _DISTINCTIVE[m]
        # EF verbs are .NET-only; suppress them in a known non-.NET file (name collision).
        if not (db == "entity_framework" and language is not None and language not in _DOTNET):
            return db
    receiver = low.rsplit(".", 1)[0].rsplit(".", 1)[-1] if "." in low else ""
    if m in _ES_VERBS and receiver and (receiver.endswith("client") or receiver in _ES_RECEIVERS):
        return "elasticsearch"  # e.g. this.client.search(dsl) / esClient.bulk(...)
    if m in _GENERIC:
        for needle, hint in _RECEIVER_HINTS:  # positive vendor hint wins
            if needle in low:
                return hint
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
