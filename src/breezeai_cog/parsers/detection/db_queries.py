"""ORM / database method-call detection (language-agnostic).

Returns an inferred ``dataAccessHint`` (the DB/ORM) for a normalized call, else
``None``. Detection is heuristic (``dataAccessHint`` is documented as inferred):
distinctive method names map directly; ambiguous ORM verbs (``findOne``/``save``…)
are refined by receiver hints.

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

# Django queryset verbs (ambiguous alone -> require a queryset-ish receiver).
_DJANGO_VERBS = {"filter", "get", "all", "exclude", "annotate", "values"}

# Neo4j runs Cypher via ``session.run(...)`` / ``tx.run(...)``. ``run`` is too generic to
# list as a distinctive method, so require a driver/session/transaction-ish receiver.
_NEO4J_RUN_RECEIVERS = ("session", "tx", "transaction", "driver", "neo4j")


def match_db(callee: str, method: str) -> str | None:
    m = method.lower()
    low = callee.lower()
    if m in _DISTINCTIVE:
        return _DISTINCTIVE[m]
    if m in _GENERIC:
        for needle, hint in _RECEIVER_HINTS:
            if needle in low:
                return hint
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
