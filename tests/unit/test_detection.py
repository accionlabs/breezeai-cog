"""Shared detection: classifier unit tests + per-language integration."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.detection import classify_call
from breezeai_cog.parsers.python.parser import PythonParser
from breezeai_cog.parsers.typescript.parser import TypeScriptParser


def test_classify_api() -> None:
    assert classify_call("axios.get", "get") == ("api_call", "GET", None)
    assert classify_call("fetch", "fetch") == ("api_call", "GET", None)
    assert classify_call("this.http.post", "post") == ("api_call", "POST", None)
    assert classify_call("requests.get", "get") == ("api_call", "GET", None)
    assert classify_call("session.delete", "delete") == ("api_call", "DELETE", None)


def test_session_query_chain_is_db_not_http() -> None:
    # `session` is both an HTTP client hint (requests.Session) and a SQLAlchemy DB
    # session. A chain carrying an ORM query-builder marker is data access, not HTTP,
    # even though it rides on `session` and ends in an HTTP verb.
    assert classify_call("session.query(User).filter(x).delete", "delete") == \
        ("db_method_call", "delete", "sqlalchemy")
    assert classify_call("session.query(User).filter(x).delete", "delete")[0] != "api_call"
    # a bare session.delete (requests.Session) stays an HTTP call
    assert classify_call("session.delete", "delete") == ("api_call", "DELETE", None)


def test_classify_db() -> None:
    assert classify_call("user.findMany", "findMany") == ("db_method_call", "findMany", "prisma")
    assert classify_call("Model.findAll", "findAll") == ("db_method_call", "findAll", "sequelize")
    assert classify_call("this.repo.findOne", "findOne") == ("db_method_call", "findOne", "typeorm")
    assert classify_call("db.session.query", "query") == ("db_method_call", "query", "sqlalchemy")
    assert classify_call("User.objects.filter", "filter") == ("db_method_call", "filter", "django")
    assert classify_call("Order.findOne", "findOne") == ("db_method_call", "findOne", "orm")


def test_classify_db_recall() -> None:
    # DBs/ORMs added for recall parity with the legacy DB_METHOD_MAP.
    assert classify_call("client.hset", "hset") == ("db_method_call", "hset", "redis")
    assert classify_call("ddb.putItem", "putItem") == ("db_method_call", "putItem", "dynamodb")
    assert classify_call("session.writeTransaction", "writeTransaction") == \
        ("db_method_call", "writeTransaction", "neo4j")
    assert classify_call("db.getDoc", "getDoc") == ("db_method_call", "getDoc", "firebase")
    assert classify_call("ctx.Users.ToListAsync", "ToListAsync") == \
        ("db_method_call", "ToListAsync", "entity_framework")
    assert classify_call("qs.select_related", "select_related") == \
        ("db_method_call", "select_related", "django")
    assert classify_call("es.msearch", "msearch") == ("db_method_call", "msearch", "elasticsearch")
    assert classify_call("Model.findAndCountAll", "findAndCountAll") == \
        ("db_method_call", "findAndCountAll", "sequelize")
    assert classify_call("coll.bulkWrite", "bulkWrite") == ("db_method_call", "bulkWrite", "mongodb")
    # Neo4j executes Cypher via session.run(...) / tx.run(...) — 'run' needs a driver-ish receiver.
    assert classify_call("session.run", "run") == ("db_method_call", "run", "neo4j")
    assert classify_call("tx.run", "run") == ("db_method_call", "run", "neo4j")
    assert classify_call("this.session.run", "run") == ("db_method_call", "run", "neo4j")
    # generic .run() on a non-driver receiver stays unclassified (avoids false positives)
    assert classify_call("context.run", "run") is None
    assert classify_call("jobRunner.run", "run") is None


def test_classify_api_recall() -> None:
    # HTTP clients added for recall parity with the legacy API_CLIENT_NAMES.
    assert classify_call("restTemplate.getForObject", "getForObject") is None  # not an HTTP verb
    assert classify_call("this.httpService.post", "post") == ("api_call", "POST", None)
    assert classify_call("webClient.get", "get") == ("api_call", "GET", None)
    assert classify_call("axios.request", "request") == ("api_call", "REQUEST", None)
    assert classify_call("$fetch", "$fetch") == ("api_call", "GET", None)
    assert classify_call("useFetch", "useFetch") == ("api_call", "GET", None)
    # bare .request() without a client hint is NOT an API call (precision)
    assert classify_call("emitter.request", "request") is None


def test_classify_none() -> None:
    assert classify_call("logger.info", "info") is None
    assert classify_call("foo.bar", "bar") is None
    # generic .get() on a non-queryset receiver stays unclassified (get needs objects/queryset)
    assert classify_call("map.get", "get") is None


def _ctx(path, src, repo):
    return ParseContext(path=path, abs_path=Path(repo) / path, source=src, repo_root=Path(repo),
                        capture_statements=True, text_truncation_limit=1000)


def test_python_statement_detection(tmp_path) -> None:
    src = (
        b"import requests\n"
        b"def f():\n"
        b"    r = requests.get('https://api.x/items')\n"
        b"    rows = User.objects.filter(active=True)\n"
        b"    return r\n"
    )
    rec = PythonParser().parse_file(_ctx("svc.py", src, tmp_path))
    by_sem = {s.semanticType: s for s in rec.statements if s.semanticType}
    assert by_sem["api_call"].method == "GET" and by_sem["api_call"].endpoint == "https://api.x/items"
    assert by_sem["db_method_call"].dataAccessHint == "django"


def test_typescript_statement_detection(tmp_path) -> None:
    src = (
        b"async function f() {\n"
        b"  const r = await axios.get('/api/x');\n"
        b"  const u = await this.repo.findOne(1);\n"
        b"  return r;\n"
        b"}\n"
    )
    rec = TypeScriptParser().parse_file(_ctx("svc.ts", src, tmp_path))
    by_sem = {s.semanticType: s for s in rec.statements if s.semanticType}
    assert by_sem["api_call"].method == "GET" and by_sem["api_call"].endpoint == "/api/x"
    assert by_sem["db_method_call"].dataAccessHint == "typeorm"


def test_query_statement_detection() -> None:
    from breezeai_cog.parsers.detection import classify_call, text_has_query

    # raw SQL / strong query builders → query_statement (before ORM db_method_call)
    assert classify_call("db.query", "query", "SELECT id FROM users")[0] == "query_statement"
    assert classify_call("em.createNativeQuery", "createNativeQuery", None)[0] == "query_statement"
    assert classify_call("p.$queryRaw", "$queryRaw", None)[0] == "query_statement"
    # ORM method (no SQL) stays db_method_call
    assert classify_call("this.repo.findById", "findById", None)[0] == "db_method_call"
    # SQL string literal embedded in a statement's source
    assert text_has_query('String sql = "SELECT u FROM User u WHERE u.id = :id";')
    # false positives guarded: UI text / leading keyword without structure
    assert classify_call("res.send", "send", "Create account") is None
    assert not text_has_query('const label = "Update your profile";')
    # Cypher/DDL with an index-type qualifier between CREATE and INDEX (Neo4j, SQL UNIQUE)
    assert classify_call("session.run", "run", "CREATE VECTOR INDEX idx FOR (n:Doc) ON n.embedding")[0] == "query_statement"
    assert classify_call("db.exec", "exec", "CREATE UNIQUE INDEX idx ON t (c)")[0] == "query_statement"
    assert text_has_query("const q = 'CREATE VECTOR INDEX idx FOR (n:Doc) ON n.embedding';")
    # still not fooled by natural language beginning with CREATE
    assert classify_call("res.send", "send", "Create a new vector for me") is None


def test_generic_verb_non_db_receiver_guarded() -> None:
    # #8: a generic ORM verb (save/delete/create/remove) on a clearly non-DB receiver
    # (cache/collection/UI-state/emitter/factory) is NOT data access.
    for callee, method in [
        ("formState.save", "save"), ("cache.delete", "delete"),
        ("emitter.remove", "remove"), ("figureFactory.create", "create"),
        ("cartItems.remove", "remove"), ("this.userCache.delete", "delete"),
    ]:
        assert classify_call(callee, method) is None, callee
    # true positives preserved: explicit repo/session, plain document saves, distinctive methods
    assert classify_call("orderRepo.save", "save") == ("db_method_call", "save", "typeorm")
    assert classify_call("user.save", "save") == ("db_method_call", "save", "orm")
    assert classify_call("userStore.save", "save") == ("db_method_call", "save", "orm")
    assert classify_call("redisCache.hget", "hget") == ("db_method_call", "hget", "redis")
