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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

# DB/ORM -> distinctive method names (lowercased). Order matters: on a name collision
# the first DB wins (mirrors the legacy reverse-map "first one wins").
_DB_METHODS: dict[str, tuple[str, ...]] = {
    "sql": ("executemany",),
    "sequelize": ("findall", "findbypk", "bulkcreate", "findorcreate", "findandcountall"),
    "prisma": ("findfirst", "findunique", "findmany"),
    "typeorm": ("createquerybuilder", "getrepository", "upsert", "findandcount"),
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
        # Async LINQ has no in-memory-collection equivalent, so `*Async` is EF on sight.
        "tolistasync", "toarrayasync",
        "firstordefaultasync", "firstasync",
        "singleordefaultasync", "singleasync",
        "lastordefaultasync",
        "countasync", "longcountasync", "anyasync", "allasync",
        "minasync", "maxasync", "sumasync", "averageasync",
        "findasync", "addasync", "addrangeasync", "savechangesasync", "savechanges",
        # EF-distinctive operators (no LINQ-to-Objects collision).
        "include", "theninclude", "asnotracking", "astracking",
        "fromsqlraw", "fromsqlinterpolated", "executesqlraw", "executesqlinterpolated",
    ),
    # NOTE: the synchronous LINQ terminals `tolist`/`toarray`/`firstordefault`/
    # `singleordefault`/`lastordefault` are deliberately NOT here — they collide with
    # LINQ-to-Objects over in-memory collections (List/array/ZipArchive). They are matched
    # in `match_db` via `_EF_LINQ_VERBS`, gated on a positive EF/queryable source in the
    # call chain, else dropped (precision-first, like `_HIGH_COLLISION`).
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

# ── language families ─────────────────────────────────────────────────────────
# Used to keep a product-specific hint out of files where that product cannot exist.
# In every gate below, an unknown language (``None``) stays permissive: the gates only
# ever fire on a language we positively know, so behaviour is unchanged for callers that
# don't pass one.

# Languages whose files may legitimately use Entity Framework verbs. EF method names
# (``ToListAsync``/``Include``/…) collide with unrelated code in other stacks — most
# painfully ``.include()``, which is TypeORM/array/RxJS in JS but EF in C#. When we KNOW
# the file is a non-.NET language we treat the EF match as a collision and fall through.
_DOTNET = frozenset({"csharp", "vb"})

# Django is Python-only; suppress Django-queryset verbs in known non-Python files so that
# TypeScript ``response.objects.filter()`` is not mislabelled as Django (mirrors the EF gate).
# Also scopes SQLAlchemy, whose ``session`` receiver is a Hibernate ``Session`` on the JVM.
_PYTHON = frozenset({"python"})

# Node-only ORMs (TypeORM, Sequelize, Prisma, Mongoose). Their receiver names
# (``…repository``, ``entityManager``) are equally idiomatic in Java/C#/Python, so the
# receiver alone cannot pick the product — only the language can rule it out.
_JSTS = frozenset({"typescript", "javascript"})

# ── Layer 2: the product named by the file's own imports ──────────────────────
# Import-prefix -> hint. Where the language gate (Layer 1) can only say "some ORM", an
# import is direct evidence of *which* one: a file that imports ``org.hibernate.Session``
# is doing Hibernate, not "generic orm". Matched case-insensitively against each entry of
# ``FileRecord.externalImports``, which every language emits as a dotted namespace
# (``org.hibernate.Session``, ``Microsoft.EntityFrameworkCore``, ``sqlalchemy.orm``) or a
# bare module (``typeorm``, ``@prisma/client``).
#
# Ordered: the first needle that matches any import wins, so a more specific namespace must
# precede its parent (spring-data-mongodb before spring-data-jpa). Deliberately conservative
# — an ambiguous package is omitted rather than guessed, leaving the generic ``orm``.
_VENDOR_BY_IMPORT: tuple[tuple[str, str], ...] = (
    # JVM — Spring Data's per-store modules are more specific than the JPA/Hibernate pair
    ("org.springframework.data.mongodb", "mongodb"),
    ("org.springframework.data.redis", "redis"),
    ("org.springframework.data.elasticsearch", "elasticsearch"),
    ("org.springframework.data.cassandra", "sql"),
    ("org.springframework.data.jpa", "jpa"),
    ("org.hibernate", "hibernate"),
    ("jakarta.persistence", "jpa"),
    ("javax.persistence", "jpa"),
    ("redis.clients", "redis"),          # Jedis
    ("com.mongodb", "mongodb"),
    ("org.neo4j", "neo4j"),
    ("com.amazonaws.services.dynamodbv2", "dynamodb"),
    ("software.amazon.awssdk.services.dynamodb", "dynamodb"),
    # .NET
    ("microsoft.entityframeworkcore", "entity_framework"),
    ("system.data.entity", "entity_framework"),
    ("nhibernate", "hibernate"),
    ("mongodb.driver", "mongodb"),
    ("stackexchange.redis", "redis"),
    ("amazon.dynamodbv2", "dynamodb"),
    # Python
    ("sqlalchemy", "sqlalchemy"),
    ("django", "django"),
    ("pymongo", "mongodb"),
    ("motor", "mongodb"),
    # Node
    ("typeorm", "typeorm"),
    ("sequelize", "sequelize"),
    ("@prisma", "prisma"),
    ("mongoose", "mongodb"),
    ("@aws-sdk/client-dynamodb", "dynamodb"),
    # cross-language clients
    ("mongodb", "mongodb"),
    ("ioredis", "redis"),
    ("redis", "redis"),
    ("@elastic", "elasticsearch"),
    ("elasticsearch", "elasticsearch"),
    ("neo4j", "neo4j"),
    ("@firebase", "firebase"),
    ("firebase", "firebase"),
    ("couchdb", "couchdb"),
    ("nano", "couchdb"),
)


def vendor_from_imports(external_imports: "Iterable[str] | None") -> str | None:
    """The datastore product named by a file's external imports, else ``None``.

    Layer 2 of the datastore gate: consulted only where the call shape establishes data
    access but not *which* product (see :func:`match_db`). Returns at most one vendor —
    the first ``_VENDOR_BY_IMPORT`` entry matching any import — so the result is stable
    regardless of the order imports appear in the source.
    """
    if not external_imports:
        return None
    lows = [i.lower() for i in external_imports]
    for needle, hint in _VENDOR_BY_IMPORT:
        for low in lows:
            if low == needle or low.startswith(f"{needle}.") or low.startswith(f"{needle}/"):
                return hint
    return None


# Receiver substring -> (hint, languages the vendor can appear in) — refines _GENERIC.
# Needles checked via `needle in low` (full lowercased callee). Ambiguous short tokens
# ("session", "objects") require a dot-anchored match so they don't fire on *containing*
# identifiers like ``sessionFactory`` / ``productObjects``; others are distinctive enough
# (``prisma``, ``sequelize``, ``mongoose``, ``repository``) that substring is fine.
#
# Every hint here names a *product*, so it is only reachable from the languages that product
# ships for: TypeORM/Sequelize/Prisma/Mongoose are Node, SQLAlchemy/Django are Python. Without
# the language column a Java ``orderRepository.findById`` was tagged ``typeorm`` and a Hibernate
# ``session.save`` ``sqlalchemy`` — products that cannot exist in that file. The receiver still
# tells us this IS data access, so a language mismatch degrades to the generic ``orm`` rather
# than dropping the detection (same convention as the ``typed_db_ids`` gate in ``match_db``).
# ``None`` = language-neutral; an unknown language stays permissive.
_RECEIVER_HINTS: tuple[tuple[str, str, frozenset[str] | None], ...] = (
    ("prisma", "prisma", _JSTS), ("sequelize", "sequelize", _JSTS),
    ("mongoose", "mongodb", _JSTS),
    ("repository", "typeorm", _JSTS), ("repo", "typeorm", _JSTS),
    ("entitymanager", "typeorm", _JSTS),
    ("session", "sqlalchemy", _PYTHON), ("queryset", "django", _PYTHON),
    ("objects", "django", _PYTHON),
)
# Needles that must appear as a whole dot-segment (not as a prefix/suffix of a longer name).
# e.g. "session" must match "db.session.query" but NOT "sessionFactory.create".
_ANCHORED_HINTS: frozenset[str] = frozenset({"session", "objects"})

# Receiver terminal-segment suffixes that are clearly NOT a database: an opt-out generic ORM
# verb (``findOne``/``findById`` — the ``_GENERIC`` verbs that are NOT high-collision) on one
# of these is app logic, not data access, so we drop the weak ``"orm"`` fallback for them.
# Matched on the receiver's final segment by suffix (``formState`` -> ``state``, ``userCache``
# -> ``cache``). A distinctive method or a positive receiver hint (checked first) always
# overrides this. (Write verbs like ``save``/``delete``/``create`` are opt-in via
# ``_HIGH_COLLISION`` and never reach this fallback.)
_NON_DB_RECEIVERS = (
    "cache", "logger", "emitter", "eventbus", "eventemitter", "state",
    "buffer", "console", "clipboard", "factory", "list", "items", "queue", "stack",
    # HTML Canvas / 2D rendering context — ctx.save()/restore() are draw-state calls, not DB.
    "ctx", "canvas", "context2d",
)

# Django queryset verbs (ambiguous alone -> require a queryset-ish receiver).
_DJANGO_VERBS = {"filter", "get", "all", "exclude", "annotate", "values"}

# Neo4j runs Cypher via ``session.run(...)`` / ``tx.run(...)``. ``run`` is too generic to
# list as a distinctive method, so require a driver/session/transaction-ish receiver.
_NEO4J_RUN_RECEIVERS = ("session", "tx", "transaction", "driver", "neo4j")

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
# ``save`` is here too: a bare ``X.Save(...)`` collides with non-DB serialization — most
# painfully Aspose ``Document.Save(stream)`` / ``htmlDoc.Save(stream)`` (format conversion, no
# database), also ``bitmap.Save``/``xmlDoc.Save``. Real ORM saves keep matching via a positive
# receiver hint/suffix (``orderRepo.save`` -> typeorm); a bare active-record ``user.save`` now
# drops (precision-first, matching its write-verb siblings ``persist``/``merge``/``create``).
_HIGH_COLLISION = frozenset(
    {"find", "create", "update", "delete", "remove", "save", "persist", "merge", "query", "execute"}
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

# Synchronous LINQ terminal operators that are AMBIGUOUS: identical method names run over
# both a DbSet/IQueryable (EF → a SQL round-trip) and an in-memory List/array/ZipArchive
# (LINQ-to-Objects → no DB). Their async twins stay in _DISTINCTIVE (in-memory collections
# have no `*Async`). We tag these `entity_framework` only when the call chain shows a
# positive EF/queryable source (`_has_ef_source`), else drop — precision-first, mirroring
# `_HIGH_COLLISION`. Only .NET files reach this branch (gated on `language in _DOTNET`).
_EF_LINQ_VERBS = frozenset({
    "tolist", "toarray", "firstordefault", "singleordefault", "lastordefault",
})

# Substrings in the FULL callee chain that positively mark an EF/queryable source, so a sync
# LINQ terminal on it is a DB query, not LINQ-to-Objects. Scanned across the whole (lowercased)
# chain because query operators sit between the source and the terminal
# (``_context.Orders.Where(...).FirstOrDefault()`` -> ``context`` precedes the terminal). Kept
# specific to avoid re-catching an in-memory receiver (``fontSources``/``bundleZip.Entries``).
_EF_SOURCE_MARKERS = ("dbcontext", "dbset", "iqueryable", "queryable", "context.", ".set(", ".set<")


def _has_ef_source(low_callee: str) -> bool:
    return any(mk in low_callee for mk in _EF_SOURCE_MARKERS)

# ElasticSearch / OpenSearch client verbs. These collide with ordinary code (``search`` is
# ``String.prototype.search``; app repos/services expose ``.search()`` too — 270+ in one repo)
# and with HTTP (``get``/``delete``), so they are gated on an ES-*client* receiver and the
# high-collision HTTP verbs (get/update/delete/create) are deliberately excluded. ``msearch``
# stays in _DISTINCTIVE (unambiguous, no receiver needed); ``mget`` is left to Redis.
_ES_VERBS = frozenset({"search", "bulk", "index", "scroll", "reindex", "count"})
# Subset of _ES_VERBS that also collide heavily with ORM/array/generic code (``repo.count()``,
# ``.index()``): these require a receiver that is *explicitly* an ES name (in _ES_RECEIVERS),
# not merely something ending in "client" — otherwise ``httpClient.count()`` reads as ES.
_ES_COLLISION_VERBS = frozenset({"count", "index"})

# Cache/Redis service verbs — too generic alone (Map.get, Set.set), so gated on a
# cache-specific receiver, mirroring the ES verb/receiver pattern.
_CACHE_VERBS = frozenset({"get", "set", "del", "reset", "store"})
_CACHE_RECEIVERS = frozenset({
    "cache", "cachemanager", "cacheservice",
    "redisservice", "redis", "redisclient",
})
# Terminal receiver segment that identifies an ES client (``this.client`` / ``esClient`` /
# ``osClient``). Anything ending in ``client`` plus the explicit ES names — but NOT the DB
# receivers above, so ``xxxCustomRepository.search`` / ``xService.search`` are excluded.
_ES_RECEIVERS = frozenset({
    "client", "esclient", "es", "elastic", "elasticsearch",
    "opensearch", "osclient",
    "searchservice", "searchclient", "opensearchservice",  # service-wrapper patterns
})


# Prisma client API verbs. Several (`deleteMany`/`updateMany`/`upsert`/`aggregate`) collide
# with Mongo/TypeORM distinctive names, so a receiver-blind table mislabels a Prisma call as
# that other store. When the call chain is an explicit Prisma-client access (`prisma.x.op()` /
# `this.prisma.x.op()`), the vendor is unambiguous — this positive signal wins. It only ADDS
# precision: a genuine Mongo/TypeORM call has no `prisma.` in its chain, so nothing regresses.
_PRISMA_VERBS = frozenset({
    "findfirst", "findunique", "findmany", "finduniqueorthrow", "findfirstorthrow",
    "create", "createmany", "update", "updatemany", "delete", "deletemany",
    "upsert", "count", "aggregate", "groupby",
})


def _is_prisma_chain(low: str) -> bool:
    """The receiver chain is a real Prisma-client access (`prisma.` root or a `.prisma.`
    segment, e.g. `this.prisma.user.findMany`)."""
    return low.startswith("prisma.") or ".prisma." in low


def match_db(callee: str, method: str, language: str | None = None,
             typed_db_ids: "frozenset[str] | None" = None,
             datastore_vendor: str | None = None) -> str | None:
    m = method.lower()
    low = callee.lower()
    # Layer 2: the product this file's imports name (``vendor_from_imports``), used at every
    # point where the call shape proves data access but not which product — instead of the
    # generic ``orm``. A distinctive method or an in-language receiver hint is more local
    # evidence and still wins, so this only ever replaces a would-be ``orm``.
    generic = datastore_vendor or "orm"
    if m in _PRISMA_VERBS and _is_prisma_chain(low):
        return "prisma"
    if m in _DISTINCTIVE:
        db = _DISTINCTIVE[m]
        # EF verbs are .NET-only; suppress them in a known non-.NET file (name collision).
        if not (db == "entity_framework" and language is not None and language not in _DOTNET):
            return db
    # Ambiguous sync LINQ terminals (ToList/FirstOrDefault/…): EF only in a .NET file AND when
    # the call chain shows a queryable/DbContext source; else LINQ-to-Objects — drop, don't tag.
    if m in _EF_LINQ_VERBS and language in _DOTNET:
        return "entity_framework" if _has_ef_source(low) else None
    receiver = low.rsplit(".", 1)[0].rsplit(".", 1)[-1] if "." in low else ""
    # ES match is gated on the TERMINAL receiver only (the segment the verb is invoked on) —
    # never a deeper chain segment, so a bare ``…client`` further up (``prismaClient``,
    # ``apiClient``) can't hijack the call: ``this.prismaClient.user.count()`` stays out of ES.
    if m in _ES_VERBS and receiver:
        if m in _ES_COLLISION_VERBS:
            # count/index also mean ORM/array ops — demand an explicit ES receiver name,
            # a bare ``…client`` (httpClient) is not enough.
            if receiver in _ES_RECEIVERS:
                return "elasticsearch"
        elif receiver.endswith("client") or receiver in _ES_RECEIVERS:
            return "elasticsearch"  # e.g. this.client.search(dsl) / this.searchService.search(...)
    # Cache/Redis service calls — gated on the TERMINAL receiver only (like ES above), and on an
    # explicit cache/redis receiver NAME. A bare ``endswith("cache")`` is deliberately NOT used:
    # in-memory ``Map``/``LRUCache`` fields are routinely named ``…Cache`` (e.g. a DataLoader
    # ``dataLoaderCache: Map<K,V>``), and their ``.get()``/``.set()`` are memory ops, not Redis.
    # Residual: an in-memory object named exactly ``cache``/``cacheService`` still matches, and the
    # NestJS ``Cache`` abstraction may be memory-backed — resolving those needs type resolution
    # (see typed_db_ids) plus a cache-vs-redis vocabulary decision; left for a follow-up.
    if receiver and (receiver in _CACHE_RECEIVERS or receiver.endswith("redis")):
        if m in _CACHE_VERBS or m in ("delete", "remove"):
            return "redis"
    if m in _GENERIC:
        for needle, hint, langs in _RECEIVER_HINTS:  # positive vendor hint wins
            if needle not in low:
                continue
            # Anchored needles must appear as a dot-segment, not embedded in a longer name
            # (e.g. "session" must match "db.session.query" but NOT "sessionFactory.create").
            if needle in _ANCHORED_HINTS:
                if not (f".{needle}." in low or low.startswith(f"{needle}.")):
                    continue
            # The receiver established data access; the language decides whether this
            # PRODUCT is reachable. A known-mismatched language degrades to the generic
            # ``orm`` instead of naming an impossible one (Java repo != TypeORM).
            if langs is not None and language is not None and language not in langs:
                return generic
            return hint
        if m in _HIGH_COLLISION:
            # Non-DB receivers (canvas ctx, logger, cache, …) are never data access — bail
            # early before the suffix heuristic can misfire (e.g. ``ctx.save()``).
            if receiver and any(receiver.endswith(s) for s in _NON_DB_RECEIVERS):
                return None
            if typed_db_ids is not None:
                # Type-resolved gate (TypeScript class context): only match when the receiver
                # is a field explicitly typed as an ORM client — avoids name-suffix guessing.
                return generic if receiver in typed_db_ids else None
            # Opt-in heuristic (Python, non-class TS, etc.): positive DB suffix wins.
            if receiver and any(receiver.endswith(s) for s in _DB_RECEIVER_SUFFIXES):
                return generic
            return None
        if receiver and any(receiver.endswith(s) for s in _NON_DB_RECEIVERS):
            return None  # a cache/collection/UI-state/etc. receiver — not data access
        return generic
    if m in _DJANGO_VERBS and (language is None or language in _PYTHON) and (
        ".objects." in low or low.startswith("objects.") or "queryset" in low
    ):
        return "django"
    if m == "run" and "." in low:
        # callee includes the method (``session.run``); the receiver is everything before
        # it, and we match on the receiver's final segment (``this.session`` -> ``session``).
        receiver_last = low.rsplit(".", 1)[0].rsplit(".", 1)[-1]
        if any(receiver_last == r or receiver_last.endswith(r) for r in _NEO4J_RUN_RECEIVERS):
            return "neo4j"
    return None
