"""Shared detection: classifier unit tests + per-language integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.detection import classify_call
from breezeai_cog.parsers.python.parser import PythonParser
from breezeai_cog.parsers.typescript.parser import TypeScriptParser


def test_classify_api() -> None:
    assert classify_call("axios.get", "get", language="typescript") == ("api_call", "GET", None)
    assert classify_call("fetch", "fetch", language="typescript") == ("api_call", "GET", None)
    assert classify_call("this.http.post", "post", language="typescript") == ("api_call", "POST", None)
    assert classify_call("requests.get", "get", language="python") == ("api_call", "GET", None)
    assert classify_call("session.delete", "delete", language="python") == ("api_call", "DELETE", None)


def test_session_query_chain_is_db_not_http() -> None:
    # `session` is both an HTTP client hint (requests.Session) and a SQLAlchemy DB
    # session. A chain carrying an ORM query-builder marker is data access, not HTTP,
    # even though it rides on `session` and ends in an HTTP verb.
    assert classify_call("session.query(User).filter(x).delete", "delete", language="python") == \
        ("db_method_call", "delete", "sqlalchemy")
    assert classify_call("session.query(User).filter(x).delete", "delete", language="python")[0] != "api_call"
    # a bare session.delete (requests.Session) stays an HTTP call
    assert classify_call("session.delete", "delete", language="python") == ("api_call", "DELETE", None)


def test_classify_db() -> None:
    assert classify_call("user.findMany", "findMany") == ("db_method_call", "findMany", "prisma")
    assert classify_call("Model.findAll", "findAll") == ("db_method_call", "findAll", "sequelize")
    assert classify_call("this.repo.findOne", "findOne") == ("db_method_call", "findOne", "typeorm")
    assert classify_call("db.session.query", "query") == ("db_method_call", "query", "sqlalchemy")
    assert classify_call("User.objects.filter", "filter") == ("db_method_call", "filter", "django")
    assert classify_call("Order.findOne", "findOne") == ("db_method_call", "findOne", "orm")


def test_prisma_chain_wins_over_mongo_typeorm_collision() -> None:
    # deleteMany/updateMany/upsert live in Mongo's/TypeORM's distinctive tables but are also
    # Prisma verbs. On an explicit `prisma.` chain the vendor is unambiguous — must be prisma.
    from breezeai_cog.parsers.detection.db_queries import match_db

    assert match_db("prisma.user.deleteMany", "deleteMany") == "prisma"
    assert match_db("prisma.user.updateMany", "updateMany") == "prisma"
    assert match_db("this.prisma.user.upsert", "upsert") == "prisma"
    assert match_db("prisma.user.findMany", "findMany") == "prisma"
    # No prisma in the chain → genuine Mongo/TypeORM detection is unchanged.
    assert match_db("collection.deleteMany", "deleteMany") == "mongodb"
    assert match_db("repo.upsert", "upsert") == "typeorm"


def test_query_execute_require_db_receiver() -> None:
    # `query`/`execute` are high-collision in TS/JS — a bare call, or one on a non-DB
    # receiver, must NOT default to `orm` (these are the dominant false positives:
    # Apollo/API clients, Angular animations/DebugElement, command/use-case pattern).
    assert classify_call("this.api.query", "query") is None
    assert classify_call("backendService.query", "query") is None
    assert classify_call("productCatalogueApollo.query", "query") is None
    assert classify_call("debugElement.query", "query") is None
    assert classify_call("query", "query") is None  # bare query(':enter') animation
    assert classify_call("useCase.execute", "execute") is None
    assert classify_call("this.execute", "execute") is None
    assert classify_call("command.execute", "execute") is None
    # `pool`/`client` are deliberately NOT DB-receiver suffixes (worker pools, Apollo client)
    assert classify_call("workerPool.execute", "execute") is None

    # Genuine raw-SQL / ORM handles are still captured via suffix or vendor hint.
    assert classify_call("dataSource.query", "query") == ("db_method_call", "query", "orm")
    assert classify_call("queryRunner.query", "query") == ("db_method_call", "query", "orm")
    assert classify_call("connection.execute", "execute") == ("db_method_call", "execute", "orm")
    assert classify_call("conn.query", "query") == ("db_method_call", "query", "orm")
    assert classify_call("userRepository.execute", "execute") == ("db_method_call", "execute", "typeorm")
    # vendor-hint path is unaffected (checked before the high-collision gate)
    assert classify_call("db.session.query", "query") == ("db_method_call", "query", "sqlalchemy")


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
    assert classify_call("this.httpService.post", "post", language="typescript") == ("api_call", "POST", None)
    assert classify_call("webClient.get", "get", language="java") == ("api_call", "GET", None)
    assert classify_call("axios.request", "request", language="typescript") == ("api_call", "REQUEST", None)
    assert classify_call("$fetch", "$fetch", language="typescript") == ("api_call", "GET", None)
    assert classify_call("useFetch", "useFetch", language="typescript") == ("api_call", "GET", None)
    # bare .request() without a client hint is NOT an API call (precision)
    assert classify_call("emitter.request", "request", language="typescript") is None


def test_classify_none() -> None:
    assert classify_call("logger.info", "info") is None
    assert classify_call("foo.bar", "bar") is None
    # generic .get() on a non-queryset receiver stays unclassified (get needs objects/queryset)
    assert classify_call("map.get", "get") is None


def _ctx(path, src, repo):
    return ParseContext(path=path, abs_path=Path(repo) / path, source=src, repo_root=Path(repo),
                        capture_statements=True, statement_text_limit=1000)


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


def _queries(parser, name: str, src: bytes, tmp_path) -> list[str]:
    rec = parser.parse_file(_ctx(name, src, tmp_path))
    return [s.text for s in rec.statements if s.semanticType == "query_statement"]


_SPLIT_STRINGS = [
    # The SQL keyword starts one literal, the rest comes from a separate one: not a query.
    ("php", "lang.php",
     b"<?php\nreturn array(\n    'Select' => 'Choose',\n    'Open' => 'Open file',\n"
     b"    'Copy' => 'Copy from clipboard',\n);\n"),
    ("typescript", "labels.ts",
     b"export const labels = {\n  select: 'Select',\n  hint: 'Pick a value from the list',\n};\n"),
    ("javascript", "menu.js", b'const items = ["Select", "Import data from file"];\n'),
    ("java", "Items.java",
     b'class Items {\n  static final String[] ITEMS = {"Select", "Import data from file"};\n}\n'),
    ("csharp", "Items.cs",
     b'class Items {\n  void F() { Show("Select", "Import data from file"); }\n}\n'),
    ("python", "menu.py", b'MENU = ["Update", "Set as default"]\n'),
]

_REAL_SQL = [
    # One literal, or literals the language concatenates in one expression: a query.
    ("php", "repo.php", b"<?php\n$sql = 'SELECT id FROM users';\n"),
    ("php", "repo2.php", b"<?php\n$sql = 'SELECT ' . $cols . ' FROM users WHERE id = ?';\n"),
    ("typescript", "repo.ts", b"const q = `SELECT id FROM users WHERE id = ${id}`;\n"),
    ("javascript", "repo.js", b'const q = "SELECT id " + "FROM users";\n'),
    ("java", "Repo.java", b'class Repo {\n  static final String SQL = "SELECT id FROM users";\n}\n'),
    ("java", "Repo2.java",
     b'class Repo {\n  String q(String t) { return "SELECT id FROM " + t + " WHERE a = 1"; }\n}\n'),
    ("csharp", "Repo.cs",
     b'class Repo {\n  void F() {\n    sSQL = "select " + Sql.FormatSelectFields(arr) + ControlChars.CrLf\n'
     b'         + "  from vwACCOUNTS_List" + ControlChars.CrLf;\n  }\n}\n'),
    ("csharp", "Repo2.cs", b'class Repo {\n  const string Q = @"UPDATE users SET name = @n";\n}\n'),
    ("python", "repo.py", b'QUERY = "SELECT name FROM orders WHERE id = %s"\n'),
    ("python", "repo2.py", b'QUERY = ("SELECT name "\n         "FROM orders")\n'),
    ("vb", "Repo.vb",
     b'Class Repo\n  Sub F()\n    Dim s = "select " & sCols & " from vwACCOUNTS"\n  End Sub\nEnd Class\n'),
]


def _parser(lang: str):
    from breezeai_cog.parsers.csharp.parser import CSharpParser
    from breezeai_cog.parsers.java.parser import JavaParser
    from breezeai_cog.parsers.php.parser import PhpParser
    from breezeai_cog.parsers.vb.parser import VbParser

    return {"php": PhpParser, "typescript": TypeScriptParser, "javascript": TypeScriptParser,
            "java": JavaParser, "csharp": CSharpParser, "python": PythonParser, "vb": VbParser}[lang]()


@pytest.mark.parametrize(("lang", "name", "src"), _SPLIT_STRINGS, ids=[c[1] for c in _SPLIT_STRINGS])
def test_sql_keywords_from_separate_literals_are_not_a_query(lang, name, src, tmp_path) -> None:
    assert _queries(_parser(lang), name, src, tmp_path) == []


@pytest.mark.parametrize(("lang", "name", "src"), _REAL_SQL, ids=[c[1] for c in _REAL_SQL])
def test_sql_in_one_string_value_is_a_query(lang, name, src, tmp_path) -> None:
    assert len(_queries(_parser(lang), name, src, tmp_path)) == 1


def test_string_has_query() -> None:
    from breezeai_cog.parsers.detection import string_has_query
    assert string_has_query(["SELECT id FROM users"])
    assert string_has_query(["select ", None, None, "  from vwACCOUNTS"])
    assert string_has_query(["\tprint 'alter table ", None, " add X null';"])  # quoted inside
    assert not string_has_query(["Select"])
    assert not string_has_query(["Copy from clipboard"])
    assert not string_has_query(["Pick a value from the list"])


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
        ("formState.save", "save"),
        ("emitter.remove", "remove"), ("figureFactory.create", "create"),
        ("cartItems.remove", "remove"),
    ]:
        assert classify_call(callee, method) is None, callee
    # An explicit ``cache`` receiver name is tagged redis; a ``…Cache`` suffix is NOT (it is
    # commonly an in-memory Map — see test_suffix_named_cache_is_not_redis).
    assert classify_call("cache.delete", "delete") == ("db_method_call", "delete", "redis")
    assert classify_call("this.userCache.delete", "delete") is None
    # true positives preserved: explicit repo/vendor hint and distinctive methods
    assert classify_call("orderRepo.save", "save") == ("db_method_call", "save", "typeorm")
    assert classify_call("redisCache.hget", "hget") == ("db_method_call", "hget", "redis")


def test_high_collision_verbs_require_db_receiver() -> None:
    # #G3: high-collision generic verbs (find/create/update/delete/remove/…) are data access
    # ONLY on a positive DB receiver — they otherwise collide with ordinary code and were the
    # dominant db_method_call false-positive source (Array.find, dict.update, Zustand, cookies).
    for callee, method in [
        ("layers.find", "find"),            # Array.prototype.find
        ("assignedTo.find", "find"),        # Array.prototype.find
        ("config.update", "update"),        # dict/object update
        ("Object.create", "create"),        # JS builtin
        ("useStore.create", "create"),      # Zustand store factory
        ("Cookies.remove", "remove"),       # js-cookie
        ("this.list.delete", "delete"),     # collection
        ("items.remove", "remove"),
        ("docx.Save", "Save"),              # Aspose.Words Document serialization to a Stream — not DB
        ("htmlDoc.Save", "Save"),           # Aspose HTML serialization — not DB
        ("user.save", "save"),              # bare active-record save, no DB receiver → ambiguous, drop
    ]:
        assert classify_call(callee, method) is None, callee
    # Ruby class constants are not enough to establish an ActiveRecord receiver.
    for callee, method in [
        ("File.delete", "delete"),
        ("Hash.merge", "merge"),
        ("Logger.create", "create"),
        ("Process.execute", "execute"),
        ("Digest.update", "update"),
        ("Set.merge", "merge"),
        ("Marshal.save", "save"),
        ("ENV.delete", "delete"),
    ]:
        assert classify_call(callee, method, language="ruby") is None, callee
    # real ORM on these verbs still matches via a positive DB receiver / vendor hint:
    assert classify_call("userRepository.find", "find") == ("db_method_call", "find", "typeorm")
    assert classify_call("this.repo.delete", "delete") == ("db_method_call", "delete", "typeorm")
    assert classify_call("userModel.create", "create") == ("db_method_call", "create", "orm")
    assert classify_call("orderDao.update", "update") == ("db_method_call", "update", "orm")
    assert classify_call("orderRepo.save", "save") == ("db_method_call", "save", "typeorm")
    assert classify_call("prisma.user.create", "create") == ("db_method_call", "create", "prisma")
    # em.merge / session.persist (Hibernate) keep matching via receiver hints:
    assert classify_call("this.entityManager.merge", "merge") == ("db_method_call", "merge", "typeorm")


def test_ruby_active_record_distinctive_verbs_need_no_index() -> None:
    """Verbs that effectively only exist in ActiveRecord are trusted on a bare constant —
    no stdlib or common-gem class API uses them, so the receiver needs no identification."""
    for callee, method in [
        ("User.find_by", "find_by"), ("User.where", "where"), ("Post.joins", "joins"),
        ("User.includes", "includes"), ("User.pluck", "pluck"),
        ("User.destroy_all", "destroy_all"), ("User.update_all", "update_all"),
        ("User.take", "take"),
    ]:
        assert classify_call(callee, method, language="ruby") == (
            "db_method_call", method, "activerecord"
        )


def test_ruby_generic_verbs_require_an_identified_model() -> None:
    """``find``/``all``/``create``/``first``/``last`` collide with real stdlib and gem APIs
    (``Tempfile.create``, ``Settings.all``, ``Date.first``), so they only count when the
    receiver is a known model. Absent beats wrong."""
    models = frozenset({"User", "user"})
    for callee, method in [
        ("User.find", "find"), ("User.create", "create"), ("User.all", "all"),
        ("User.first", "first"), ("User.last", "last"),
        ("user.save", "save"), ("user.update", "update"), ("user.destroy", "destroy"),
    ]:
        assert classify_call(callee, method, language="ruby", typed_db_ids=models) == (
            "db_method_call", method, "activerecord"
        )
        assert classify_call(callee, method, language="ruby") is None


def test_ruby_non_activerecord_constants_are_never_data_access() -> None:
    """Real class-level APIs that share a name with an ActiveRecord verb. None is a model,
    so none may be tagged — even though every Ruby class name is capitalized."""
    for callee, method in [
        ("File.delete", "delete"), ("Hash.merge", "merge"), ("Tempfile.create", "create"),
        ("FileUtils.create", "create"), ("Pathname.create", "create"),
        ("Struct.create", "create"), ("CSV.create", "create"), ("Logger.create", "create"),
        ("Settings.all", "all"), ("Flipper.all", "all"), ("Thread.all", "all"),
        ("Set.all", "all"), ("Date.first", "first"), ("Time.last", "last"),
        ("Timeout.first", "first"), ("File.find", "find"), ("Dir.find", "find"),
        ("Gem.find", "find"), ("ENV.find", "find"), ("Process.find", "find"),
        ("File.exists", "exists"), ("Marshal.save", "save"), ("Digest.update", "update"),
        ("config.save", "save"), ("image.update", "update"),
    ]:
        assert classify_call(callee, method, language="ruby") is None, callee


def test_elasticsearch_client_verbs_gated() -> None:
    # G1: ES client verbs (search/bulk/index/scroll/count) are data access ONLY on an
    # ES-client receiver — so String.search, app repo/service .search(), etc. are excluded.
    assert classify_call("this.client.search", "search") == ("db_method_call", "search", "elasticsearch")
    assert classify_call("client.search", "search") == ("db_method_call", "search", "elasticsearch")
    assert classify_call("esClient.bulk", "bulk") == ("db_method_call", "bulk", "elasticsearch")
    assert classify_call("this.client.index", "index") == ("db_method_call", "index", "elasticsearch")
    assert classify_call("osClient.scroll", "scroll") == ("db_method_call", "scroll", "elasticsearch")
    # collisions rejected — not an ES-client receiver:
    assert classify_call("this.projectsCustomRepository.search", "search") is None  # app repo method
    assert classify_call("companyService.search", "search") is None                 # app service method
    assert classify_call("keyword.search", "search") is None                        # String.prototype.search
    # high-collision HTTP verbs deliberately excluded from ES (avoid map.get/cache.delete/api):
    assert classify_call("client.get", "get") is None
    assert classify_call("client.delete", "delete") is None


def test_entity_framework_verbs_are_dotnet_gated() -> None:
    # EF verbs collide with other stacks (esp. `.include()` = TypeORM/RxJS/array in JS).
    # Suppress them in a KNOWN non-.NET language; keep them for .NET and unknown (None).
    assert classify_call("qb.include", "include", None, "typescript") is None
    assert classify_call("query.include", "include", None, "python") is None
    assert classify_call("ctx.Users.ToListAsync", "ToListAsync", None, "typescript") is None
    # .NET languages: EF preserved
    assert classify_call("ctx.Users.ToListAsync", "ToListAsync", None, "csharp") == \
        ("db_method_call", "ToListAsync", "entity_framework")
    assert classify_call("ctx.Users.AsNoTracking", "AsNoTracking", None, "vb") == \
        ("db_method_call", "AsNoTracking", "entity_framework")
    # unknown language stays permissive (backward compatible)
    assert classify_call("ctx.Users.ToListAsync", "ToListAsync") == \
        ("db_method_call", "ToListAsync", "entity_framework")


def test_receiver_hints_name_only_products_of_the_file_language() -> None:
    from breezeai_cog.parsers.detection.db_queries import match_db
    # A repository / entity manager / session is data access in any language, but TypeORM and
    # SQLAlchemy exist only in Node and Python: elsewhere the hint is the generic `orm`.
    assert match_db("orderRepository.findById", "findById", "java") == "orm"
    assert match_db("entityManager.persist", "persist", "java") == "orm"
    assert match_db("session.save", "save", "java") == "orm"
    assert match_db("repo.Save", "Save", "csharp") == "orm"
    assert match_db("repository.create", "create", "python") == "orm"
    # In their own language, and with no language, the product is kept.
    assert match_db("orderRepository.findById", "findById", "typescript") == "typeorm"
    assert match_db("db.session.query", "query", "python") == "sqlalchemy"
    assert match_db("orderRepository.findById", "findById") == "typeorm"


def test_distinctive_verbs_of_another_language_need_a_db_receiver() -> None:
    from breezeai_cog.parsers.detection.db_queries import match_db
    # Stream, collection and claims methods share names with Prisma / Sequelize verbs.
    assert match_db("names.stream().filter(n).findFirst", "findFirst", "java") is None
    assert match_db("nums.findAll", "findAll", "groovy") is None
    assert match_db("Array.FindAll", "FindAll", "csharp") is None
    assert match_db("identity.FindFirst", "FindFirst", "csharp") is None
    # On a repository receiver it is still data access.
    assert match_db("prisma.user.findFirst", "findFirst", "java") == "orm"
    assert match_db("logRepository.findAll", "findAll", "java") == "orm"
    assert match_db("repo.findAll", "findAll", "java") == "orm"
    # In Node, and with no language, the verb alone still names the product.
    assert match_db("User.findAll", "findAll", "javascript") == "sequelize"
    assert match_db("db.user.findFirst", "findFirst", "typescript") == "prisma"
    assert match_db("repo.findAll", "findAll") == "sequelize"
    # Portable driver verbs are not gated.
    assert match_db("collection.insertOne", "insertOne", "java") == "mongodb"


def test_vendor_from_imports() -> None:
    from breezeai_cog.parsers.detection import vendor_from_imports
    assert vendor_from_imports(["org.hibernate.Session"]) == "hibernate"
    assert vendor_from_imports(["jakarta.persistence.EntityManager"]) == "jpa"
    assert vendor_from_imports(["javax.persistence.EntityManagerFactory"]) == "jpa"
    # Annotations and exceptions are shared with other ORMs (Ebean): not evidence of the API.
    assert vendor_from_imports(["jakarta.persistence.PersistenceException"]) is None
    assert vendor_from_imports(["javax.persistence.*", "org.hibernate.annotations.Type"]) is None
    # Spring Data JPA runs on Hibernate by default but is named for what the code uses.
    assert vendor_from_imports(
        ["org.springframework.data.jpa.repository.JpaRepository", "org.hibernate.Session"]
    ) == "jpa"
    assert vendor_from_imports(["NHibernate", "System.Linq"]) == "hibernate"
    assert vendor_from_imports(["Microsoft.EntityFrameworkCore"]) == "entity_framework"
    assert vendor_from_imports(["org.hibernateish.Thing", "java.util.List"]) is None
    assert vendor_from_imports(None) is None


def test_imported_orm_names_calls_on_an_orm_handle() -> None:
    from breezeai_cog.parsers.detection.db_queries import match_db
    # Where the language left `orm`, the file's imported ORM names the product on a repository,
    # DAO, entity manager or session receiver.
    assert match_db("this.entityManager.persist", "persist", "java", vendor="jpa") == "jpa"
    assert match_db("session.save", "save", "java", vendor="hibernate") == "hibernate"
    assert match_db("orderRepository.findById", "findById", "java", vendor="jpa") == "jpa"
    assert match_db("userDao.findOne", "findOne", "java", vendor="hibernate") == "hibernate"
    assert match_db("logRepository.findAll", "findAll", "java", vendor="jpa") == "jpa"
    # Data access on another kind of receiver stays generic: the import is about the file.
    assert match_db("items.collection.remove", "remove", "csharp", vendor="hibernate") == "orm"
    assert match_db("userModel.findOne", "findOne", "java", vendor="jpa") == "orm"
    # The vendor never creates a detection nor overrides a named product.
    assert match_db("names.stream().findFirst", "findFirst", "java", vendor="jpa") is None
    assert match_db("orderRepository.findById", "findById", "typescript", vendor="jpa") == "typeorm"
    assert match_db("collection.insertOne", "insertOne", "java", vendor="jpa") == "mongodb"


def test_java_and_csharp_files_name_their_imported_orm(tmp_path) -> None:
    from breezeai_cog.parsers.csharp.parser import CSharpParser
    from breezeai_cog.parsers.java.parser import JavaParser

    java = (
        b"import jakarta.persistence.EntityManager;\n"
        b"class OrderStore {\n"
        b"  EntityManager entityManager;\n"
        b"  void add(Order o) { entityManager.persist(o); }\n"
        b"}\n"
    )
    rec = JavaParser().parse_file(_ctx("OrderStore.java", java, tmp_path))
    assert [s.dataAccessHint for s in rec.statements if s.semanticType == "db_method_call"] == ["jpa"]
    plain = java.replace(b"import jakarta.persistence.EntityManager;\n", b"")
    rec = JavaParser().parse_file(_ctx("Plain.java", plain, tmp_path))
    assert [s.dataAccessHint for s in rec.statements if s.semanticType == "db_method_call"] == ["orm"]

    cs = (
        b"using NHibernate;\n"
        b"class OrderStore {\n"
        b"  ISession session;\n"
        b"  void Add(Order o) { session.Save(o); }\n"
        b"}\n"
    )
    rec = CSharpParser().parse_file(_ctx("OrderStore.cs", cs, tmp_path))
    assert [s.dataAccessHint for s in rec.statements if s.semanticType == "db_method_call"] == [
        "hibernate"
    ]


def test_dynamodb_dotnet_async_operations_are_distinctive() -> None:
    from breezeai_cog.parsers.detection.db_queries import match_db

    assert match_db("_dynamo.PutItemAsync", "PutItemAsync", "csharp") == "dynamodb"
    assert match_db("client.BatchWriteItemAsync", "BatchWriteItemAsync", "csharp") == "dynamodb"
    assert match_db("client.TransactWriteItemsAsync", "TransactWriteItemsAsync", "vb") == "dynamodb"
    assert match_db("PutItemAsync", "PutItemAsync", "csharp") is None  # no receiver: a local call


def test_typed_datastore_receivers_name_the_product() -> None:
    from breezeai_cog.parsers.detection.db_queries import match_db

    typed = {"_ctx": "dynamodb", "Orders": "dynamodb"}
    assert match_db("_ctx.SaveAsync", "SaveAsync", "csharp", typed_receivers=typed) == "dynamodb"
    assert match_db("this._ctx.LoadAsync", "LoadAsync", "csharp", typed_receivers=typed) == \
        "dynamodb"
    assert match_db("Orders.Query", "Query", "csharp", typed_receivers=typed) == "dynamodb"
    # The verb alone is not evidence: another receiver, or a verb the product doesn't have.
    assert match_db("_cache.SaveAsync", "SaveAsync", "csharp", typed_receivers=typed) is None
    assert match_db("_cache.SaveAsync", "SaveAsync", "csharp") is None
    assert match_db("_ctx.Dispose", "Dispose", "csharp", typed_receivers=typed) is None


def test_csharp_dynamodb_clients_are_recognised_by_declared_type(tmp_path) -> None:
    from breezeai_cog.parsers.csharp.parser import CSharpParser

    def hints(src: bytes) -> list[tuple[str, str | None]]:
        rec = CSharpParser().parse_file(_ctx("Store.cs", src, tmp_path))
        return [(s.method or "", s.dataAccessHint) for s in rec.statements
                if s.semanticType == "db_method_call"]

    body = (
        b"class OrderStore {\n"
        b"  private readonly IDynamoDBContext _ctx;\n"
        b"  private readonly ICache _cache;\n"
        b"  private Table Orders { get; }\n"
        b"  async Task Save(Order o, IAmazonDynamoDB dynamo) {\n"
        b"    await _ctx.SaveAsync(o);\n"
        b"    await _cache.SaveAsync(o);\n"
        b"    await Orders.GetItemAsync(o.Id);\n"
        b"    await dynamo.QueryAsync(request);\n"
        b"    var client = new AmazonDynamoDBClient();\n"
        b"    await client.ScanAsync(scan);\n"
        b"  }\n"
        b"}\n"
    )
    assert hints(b"using Amazon.DynamoDBv2;\nusing Amazon.DynamoDBv2.DataModel;\n" + body) == [
        ("SaveAsync", "dynamodb"), ("GetItemAsync", "dynamodb"), ("QueryAsync", "dynamodb"),
        ("ScanAsync", "dynamodb"),
    ]
    # Without the SDK import (global usings), the DynamoDB-named types still count; a class
    # named ``Table`` does not.
    assert hints(body) == [
        ("SaveAsync", "dynamodb"), ("QueryAsync", "dynamodb"), ("ScanAsync", "dynamodb"),
    ]
    assert hints(body.replace(b"DynamoDB", b"Document")) == []


def test_ef_include_needs_an_ef_import_or_source(tmp_path) -> None:
    from breezeai_cog.parsers.csharp.parser import CSharpParser
    from breezeai_cog.parsers.detection.db_queries import match_db

    # Script and style bundles share EF's eager-loading name.
    assert match_db("bndSplendidScripts.Include", "Include", "csharp") is None
    assert match_db('new ScriptBundle("~/b").Include', "Include", "csharp") is None
    assert match_db("register.Include", "Include", "vb") is None
    # EF: the file imports it, or the chain starts at a context / DbSet / IQueryable.
    assert match_db("query.Include", "Include", "csharp", vendor="entity_framework") == \
        "entity_framework"
    assert match_db("dbContext.Speakers.Where(…).Include", "Include", "csharp") == \
        "entity_framework"
    assert match_db("context.Enrollments.Include(…).ThenInclude", "ThenInclude", "csharp") == \
        "entity_framework"
    assert match_db("query.Include", "Include", "csharp", vendor="hibernate") is None

    def hints(src: bytes) -> list[str | None]:
        rec = CSharpParser().parse_file(_ctx("Svc.cs", src, tmp_path))
        return [s.dataAccessHint for s in rec.statements if s.semanticType == "db_method_call"]

    body = (
        b"class Svc {\n"
        b"  object Load(IQueryable<Region> query) { return query.Include(x => x.Parent); }\n"
        b"}\n"
    )
    assert hints(b"using Microsoft.EntityFrameworkCore;\n" + body) == ["entity_framework"]
    assert hints(b"using System.Data.Entity;\n" + body) == ["entity_framework"]
    assert hints(
        b"using System.Web.Optimization;\n"
        b"class BundleConfig {\n"
        b'  static void Register(BundleCollection bundles) {\n'
        b'    bundles.Add(new ScriptBundle("~/bundles/jquery").Include("~/Scripts/jquery.js"));\n'
        b"  }\n"
        b"}\n"
    ) == []


def test_sync_linq_terminals_require_ef_source() -> None:
    # Sync LINQ terminals (ToList/FirstOrDefault/…) run over BOTH a DbSet (EF → SQL) and an
    # in-memory List/array/ZipArchive (LINQ-to-Objects → no DB). Tag EF only with a queryable
    # source in the chain, else drop — these are the HUBS-201 / HUBS-196 false positives.
    for callee, m in [
        ("doc.Sections.ToList", "ToList"),                 # Aspose node list
        ("bundleZip.Entries.SingleOrDefault", "SingleOrDefault"),  # ZipArchive
        ("fontSources.ToArray", "ToArray"),                # List<>
        ("candidates.FirstOrDefault", "FirstOrDefault"),   # local List
    ]:
        assert classify_call(callee, m, None, "csharp") is None, (callee, m)

    # Genuine EF: a DbContext/DbSet/queryable source anywhere in the chain → entity_framework.
    assert classify_call("dbContext.Users.ToList", "ToList", None, "csharp") == \
        ("db_method_call", "ToList", "entity_framework")
    # query operators between source and terminal don't hide the source (whole-chain scan).
    assert classify_call("_context.Orders.Where(o => o.Paid).FirstOrDefault", "FirstOrDefault",
                         None, "csharp") == ("db_method_call", "FirstOrDefault", "entity_framework")
    assert classify_call("repo.Set<User>().ToList", "ToList", None, "csharp") == \
        ("db_method_call", "ToList", "entity_framework")

    # Async LINQ has no in-memory twin → stays unconditional EF (still .NET-gated elsewhere).
    assert classify_call("items.ToListAsync", "ToListAsync", None, "csharp") == \
        ("db_method_call", "ToListAsync", "entity_framework")
    # In a non-.NET file these are never EF (an in-memory .toList() in JS, etc.).
    assert classify_call("dbContext.Users.ToList", "ToList", None, "typescript") is None


def test_array_filter_on_productobjects_not_django() -> None:
    # Gap 3: Array.filter() on a variable containing "objects" in its name (e.g.
    # productObjects, digitalObjects) must NOT be tagged as django. The fix checks
    # for ".objects." (Django queryset manager segment) not substring "objects".
    from breezeai_cog.parsers.detection.db_queries import match_db
    assert match_db("r?.productObjects?.filter", "filter") is None
    assert match_db("digitalObjects?.filter", "filter") is None
    assert match_db("items.filter", "filter") is None
    # Real Django patterns must still match
    assert match_db("MyModel.objects.filter", "filter") == "django"
    assert match_db("queryset.filter", "filter") == "django"
    assert match_db("User.objects.all", "all") == "django"


def test_django_verbs_gated_to_python() -> None:
    # Django queryset verbs (.filter/.get/.all) must NOT fire in TypeScript files —
    # TS code with `.objects.filter()` is JS array/object manipulation, not Django.
    assert classify_call("data.objects.filter", "filter", None, "typescript") is None
    assert classify_call("response.objects.get", "get", None, "typescript") is None
    # Python files: Django still works
    assert classify_call("MyModel.objects.filter", "filter", None, "python") == \
        ("db_method_call", "filter", "django")
    # unknown language stays permissive (backward compat)
    assert classify_call("MyModel.objects.filter", "filter") == \
        ("db_method_call", "filter", "django")


def test_opensearch_service_receivers_detected() -> None:
    # OpenSearch/ES calls through service wrappers must be detected.
    assert classify_call("this.searchService.search", "search") == \
        ("db_method_call", "search", "elasticsearch")
    assert classify_call("this.searchClient.bulk", "bulk") == \
        ("db_method_call", "bulk", "elasticsearch")
    assert classify_call("this.opensearchService.count", "count") == \
        ("db_method_call", "count", "elasticsearch")
    # existing ES patterns still work
    assert classify_call("this.client.search", "search") == \
        ("db_method_call", "search", "elasticsearch")


def test_non_terminal_client_segment_does_not_hijack_es() -> None:
    # A bare ``…client`` deeper in the chain must not pull an ORM/HTTP verb into ES.
    # ``count`` isn't a _GENERIC verb, so these fall through to None (never elasticsearch).
    assert classify_call("this.prismaClient.user.count", "count") is None
    assert classify_call("this.apiClient.users.index", "index") is None
    # Terminal ``…client`` + a collision verb (count/index) is also not enough on its own —
    # only an explicit ES receiver name qualifies.
    assert classify_call("httpClient.count", "count") is None
    assert classify_call("this.restClient.index", "index") is None
    # …but explicit ES receivers with a collision verb still match.
    assert classify_call("this.client.index", "index") == \
        ("db_method_call", "index", "elasticsearch")
    assert classify_call("this.opensearchService.count", "count") == \
        ("db_method_call", "count", "elasticsearch")
    # Distinctive ES verbs (search/bulk/scroll) still accept any ``…client`` receiver.
    assert classify_call("esClient.bulk", "bulk") == \
        ("db_method_call", "bulk", "elasticsearch")


def test_cache_redis_calls_detected() -> None:
    # Cache/Redis service .get()/.set()/.del() must be tagged as redis.
    assert classify_call("this.cacheService.get", "get") == \
        ("db_method_call", "get", "redis")
    assert classify_call("this.cacheManager.set", "set") == \
        ("db_method_call", "set", "redis")
    assert classify_call("this.cacheService.del", "del") == \
        ("db_method_call", "del", "redis")
    assert classify_call("this.redisService.get", "get") == \
        ("db_method_call", "get", "redis")
    assert classify_call("this.cacheService.delete", "delete") == \
        ("db_method_call", "delete", "redis")
    # plain Map.get / Set.set stay unclassified (no cache receiver)
    assert classify_call("map.get", "get") is None
    assert classify_call("settings.set", "set") is None


def test_suffix_named_cache_is_not_redis() -> None:
    # A bare ``…Cache`` suffix no longer implies redis: in-memory Map/LRU fields are routinely
    # named ``…Cache`` (dogfooded on nimbus-api: ``dataLoaderCache: Map<K, DataLoader>``), and
    # their ``.get()``/``.set()`` are memory ops, not Redis. Only explicit cache/redis receiver
    # names — or a ``…redis`` suffix — classify.
    assert classify_call("this.dataLoaderCache.get", "get") is None  # Map<K, DataLoader>
    assert classify_call("this.productCache.set", "set") is None
    assert classify_call("this.responseCache.delete", "delete") is None
    # a ``…cache`` segment deeper in the chain also stays out (terminal receiver is entries/items)
    assert classify_call("this.responseCache.entries.delete", "delete") is None
    assert classify_call("this.pageCache.items.get", "get") is None
    # …but explicit cache/redis receiver names still classify.
    assert classify_call("this.cacheService.get", "get") == \
        ("db_method_call", "get", "redis")
    assert classify_call("this.userRedis.get", "get") == \
        ("db_method_call", "get", "redis")


def test_dataaccess_false_positives() -> None:
    """Regression: 4 false positive dataAccessHint patterns from real repos."""
    from breezeai_cog.parsers.detection.db_queries import match_db

    # 1. Canvas ctx.save() / canvas.save() must NOT be ORM (draw-state call).
    assert match_db("ctx.save", "save") is None
    assert match_db("canvas.save", "save") is None
    assert match_db("this.ctx.save", "save") is None

    # 2. TypeORM repository.upsert() must be "typeorm", not "sequelize".
    assert match_db("this.userRepository.upsert", "upsert") == "typeorm"
    assert match_db("repository.upsert", "upsert") == "typeorm"

    # 3. JS Array.find() on a non-DB collection must not match.
    assert match_db("formFields.find", "find") is None

    # 4. sessionFactory.create() must NOT be tagged sqlalchemy — "session" is a compound
    #    name, not a standalone SQLAlchemy session. Anchored needle check prevents this.
    assert match_db("sessionFactory.create", "create") is None
    assert match_db("sessionData.find", "find") is None

    # Real SQLAlchemy session queries must still match.
    assert match_db("db.session.query", "query") == "sqlalchemy"
    assert match_db("session.query", "query") == "sqlalchemy"


def test_typed_db_ids_gate_high_collision_verbs(tmp_path) -> None:
    """TypeScript class methods: HIGH_COLLISION verbs only fire on ORM-typed receivers.

    ``stateManager.find()`` / ``itemsCollection.create()`` must not emit db_method_call
    when the class has no ORM-typed fields. ``userRepository.find()`` (typed as
    Repository<User> in the constructor) must still emit the correct ORM hit."""
    src = b"""
import { Injectable } from '@angular/core';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { User } from './user.entity';

@Injectable()
export class UserService {
  constructor(
    @InjectRepository(User)
    private userRepository: Repository<User>,
    private stateManager: StateManagerService,
  ) {}

  findAll() {
    return this.userRepository.find();   // TRUE positive: Repository<User>
  }

  findActive() {
    return this.stateManager.find();     // FALSE positive without type gate
  }

  process(items: Item[]) {
    return items.find(x => x.active);   // FALSE positive without type gate
  }
}
"""
    p = tmp_path / "user.service.ts"
    p.write_bytes(src)
    from breezeai_cog.parsers.base import ParseContext
    ctx = ParseContext(path="user.service.ts", abs_path=p, source=src,
                       repo_root=tmp_path, capture_statements=True)
    rec = TypeScriptParser().parse_file(ctx)
    db_stmts = [s for s in rec.statements if s.semanticType == "db_method_call"]
    # Only the Repository-typed receiver should produce a db_method_call.
    assert len(db_stmts) == 1
    assert db_stmts[0].dataAccessHint in ("typeorm", "orm")  # repository hint wins


def test_inject_repository_decorator_gates_db_calls(tmp_path) -> None:
    """@InjectRepository(Entity) without an explicit Repository<> type annotation
    must still gate HIGH_COLLISION verbs as ORM calls."""
    src = b"""
import { Injectable } from '@angular/core';
import { InjectRepository } from '@nestjs/typeorm';

@Injectable()
export class TaskService {
  constructor(
    @InjectRepository(Task)
    private taskRepo,
  ) {}

  findAll() {
    return this.taskRepo.find();
  }
}
"""
    p = tmp_path / "task.service.ts"
    p.write_bytes(src)
    ctx = ParseContext(path="task.service.ts", abs_path=p, source=src,
                       repo_root=tmp_path, capture_statements=True)
    rec = TypeScriptParser().parse_file(ctx)
    db_stmts = [s for s in rec.statements if s.semanticType == "db_method_call"]
    assert len(db_stmts) == 1
    assert db_stmts[0].dataAccessHint in ("typeorm", "orm")


def test_scala_false_positive_gates() -> None:
    from breezeai_cog.parsers.detection.db_queries import match_db
    from breezeai_cog.parsers.detection.api_calls import match_api

    # B1: in-memory Map named cache is not Redis in Scala
    assert match_db("cache.get", "get", language="scala") is None
    assert match_db("cache.get", "get", language="javascript") == "redis"

    # B2: bare aggregate is not MongoDB
    assert match_db("aggregate", "aggregate", language="scala") is None
    assert match_db("aggregate", "aggregate", language="javascript") is None
    assert match_db("collection.aggregate", "aggregate") == "mongodb"

    # B3: Scala HTTP client named client is not Elasticsearch
    assert match_db("client.search", "search", language="scala") is None
    assert match_db("client.index", "index", language="scala") is None
    assert match_db("esClient.search", "search", language="scala") == "elasticsearch"

    # B4: ctx.run in Scala is Quill, not Neo4j
    assert match_db("ctx.run", "run", language="scala") == "quill"
    assert match_db("context.run", "run", language="scala") == "quill"
    assert match_db("session.run", "run") == "neo4j"
    assert match_db("driver.run", "run") == "neo4j"

    # B5: Capitalized Request constructor is not an outbound api_call
    assert match_api("http4s.Request", "Request", language="scala") is None
    assert match_api("Request", "Request", language="scala") is None
    assert match_api("axios.request", "request", language="typescript") == "REQUEST"


def test_api_hints_ignore_call_arguments() -> None:
    from breezeai_cog.parsers.detection.api_calls import match_api

    # A URL literal must not turn an arbitrary receiver into an HTTP client.
    assert match_api('ws.url("https://api.example/users").get', "get", language="scala") is None
    assert match_api('builder.run("https://api.example/users")', "run") is None
    # Receiver hints still work through ordinary member chains.
    assert match_api("this.http.post", "post", language="typescript") == "POST"
    assert match_api("axios.get", "get", language="typescript") == "GET"


def test_dotnet_session_remove_is_session_state() -> None:
    # ASP.NET session state is neither data access nor an HTTP client, whatever the file's ORM.
    for vendor in (None, "entity_framework", "hibernate"):
        assert classify_call("HttpContext.Session.Remove", "Remove", language="csharp",
                             vendor=vendor) is None
        assert classify_call("Session.Remove", "Remove", language="vb", vendor=vendor) is None
    assert classify_call("Session.Get", "Get", language="csharp") is None
    assert classify_call("HttpContext.Session.Get", "Get", language="csharp") is None
    # NHibernate / RavenDB sessions (they Delete, never Remove) stay data access: NHibernate
    # when the file imports it, else the generic orm, never the file's EF.
    nh = {"language": "csharp", "vendor": "hibernate"}
    assert classify_call("session.Delete", "Delete", **nh) == ("db_method_call", "Delete", "hibernate")
    assert classify_call("this.session.Save", "Save", **nh) == ("db_method_call", "Save", "hibernate")
    assert classify_call("session.Query", "Query", language="csharp",
                         vendor="entity_framework") == ("db_method_call", "Query", "orm")
    # Other languages keep their session readings.
    assert classify_call("session.query", "query", language="python")[2] == "sqlalchemy"
    assert classify_call("requests.Session.get", "get", language="python")[0] == "api_call"


def test_php_classifier_does_not_guess_plain_receivers() -> None:
    from breezeai_cog.parsers.detection import classify_call

    assert classify_call("builder.where", "where", language="php") is None
    assert classify_call("this.model.findAll", "findAll", language="php") is None
    assert classify_call("cache.get", "get", language="php") is None
    assert classify_call("stmt.fetch", "fetch", language="php") is None
    assert classify_call("this.client.fetch", "fetch", language="php") is None
    # PHP HTTP calls come from the evidence-based PHP HTTP-call detectors, not names.
    assert classify_call("http.get", "get", language="php") is None


def test_c_family_classifier_does_not_guess_from_names() -> None:
    for language in ("c", "cpp"):
        assert classify_call("ctx_http.post", "post", language=language) is None
        assert classify_call("sessions.get", "get", language=language) is None
        assert classify_call("session.run", "run", language=language) is None
        assert classify_call("models.remove", "remove", language=language) is None
        # Raw SQL keeps its structural check in every language.
        assert classify_call("db.exec", "exec", "SELECT id FROM t WHERE id = 1",
                             language=language) == ("query_statement", "exec", None)


def test_php_cannot_activate_javascript_orm_mappings() -> None:
    from breezeai_cog.parsers.detection import classify_call

    for callee, method in (
        ("Model.findAll", "findAll"),
        ("repository.upsert", "upsert"),
        ("ddb.putItem", "putItem"),
    ):
        assert classify_call(callee, method, language="php") is None



@pytest.mark.parametrize(
    "text, is_sql",
    [
        ("TRUNCATE TABLE users", True),
        ("truncate users;", True),
        ("TRUNCATE users", True),
        ("TRUNCATE ONLY public.users CASCADE", True),
        ("TRUNCATE users RESTART IDENTITY", True),
        ("Truncate nothing in AOF %s to timestamp %ld\n", False),  # a C format string
        ("Truncate the log", False),
    ],
)
def test_truncate_needs_sql_structure(text: str, is_sql: bool) -> None:
    from breezeai_cog.parsers.detection.queries import is_query

    assert is_query("printf", text) is is_sql


def test_bundling_include_is_not_entity_framework() -> None:
    from breezeai_cog.parsers.detection.db_queries import match_db
    bundle = 'new ScriptBundle("~/bundles/app").Include'
    assert match_db(bundle, "Include", "csharp", vendor="entity_framework") is None
    assert match_db("bundle.Include", "Include", "csharp", vendor="entity_framework") is None
    # an EF source in the chain still wins over the name
    assert match_db("_context.ProductBundles.Include", "Include", "csharp") == "entity_framework"


def test_client_hints_apply_only_in_their_own_language() -> None:
    from breezeai_cog.parsers.detection.api_calls import match_api

    # `session` is Python's requests.Session; in Java / Groovy it is a JSON object or a Map.
    assert match_api("session.get", "get", language="python") == "GET"
    for lang in ("java", "groovy"):
        assert match_api("session.getString", "getString", language=lang) is None
        assert match_api("session.put", "put", language=lang) is None
        assert match_api("session.get", "get", language=lang) is None
    # The .NET HttpClient helper verbs are .NET's only.
    assert match_api("_http.GetStringAsync", "GetStringAsync", language="csharp") == "GET"
    assert match_api("http.getString", "getString", language="java") is None
    # `ky` / `got` are JS libraries, not fragments of a Java name.
    assert match_api("ky.get", "get", language="typescript") == "GET"
    assert match_api("yekyJsonArray.get", "get", language="java") is None
    # A bare `fetch` is the JS function; Ruby's `params.fetch(:id)` reads a hash.
    assert match_api("fetch", "fetch", language="typescript") == "GET"
    assert match_api("params.fetch", "fetch", language="ruby") is None
    # The JVM's own clients still match.
    assert match_api("restTemplate.delete", "delete", language="kotlin") == "DELETE"
    assert match_api("this.restTemplate.Delete", "Delete", language="csharp") == "DELETE"  # Spring.NET
    # A language without hints, or an unknown one, has no name match.
    assert match_api("axios.get", "get", language=None) is None
    assert match_api("http.get", "get", language="cpp") is None


def test_client_name_must_end_the_receiver_name() -> None:
    from breezeai_cog.parsers.detection.api_calls import match_api

    # A name ending in a client word is a client …
    assert match_api("dataApiClient.get", "get", language="java") == "GET"
    assert match_api("new P3APIClient(uri).get", "get", language="groovy") == "GET"
    assert match_api("DefaultHttpClient.PostAsync", "PostAsync", language="csharp") == "POST"
    assert match_api("this._http.post", "post", language="typescript") == "POST"
    assert match_api("this.http\n      .post", "post", language="typescript") == "POST"
    assert match_api("this.$http.get", "get", language="typescript") == "GET"
    assert match_api("http_client.get", "get", language="python") == "GET"
    assert match_api("httpx.AsyncClient().get", "get", language="python") == "GET"
    assert match_api("axios.create().get", "get", language="typescript") == "GET"
    assert match_api("Net::HTTP.get", "get", language="ruby") == "GET"
    assert match_api("HTTParty.get", "get", language="ruby") == "GET"
    # … one only containing it is something else: a pool, a cache, a provider, a Map.
    assert match_api("ApiUtil.apiClientPool.get", "get", language="java") is None
    assert match_api("HttpRuntime.Cache.Get", "Get", language="csharp") is None
    assert match_api("httpContextProvider.Get", "Get", language="csharp") is None
    assert match_api("fetched.get", "get", language="typescript") is None
    # A short name must start a word: `sky` and `forgot` are not ky / got.
    assert match_api("sky.get", "get", language="typescript") is None
    assert match_api("forgot.post", "post", language="typescript") is None
