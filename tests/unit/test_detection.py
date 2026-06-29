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


def test_classify_db() -> None:
    assert classify_call("user.findMany", "findMany") == ("db_method_call", "findMany", "prisma")
    assert classify_call("Model.findAll", "findAll") == ("db_method_call", "findAll", "sequelize")
    assert classify_call("this.repo.findOne", "findOne") == ("db_method_call", "findOne", "typeorm")
    assert classify_call("db.session.query", "query") == ("db_method_call", "query", "sqlalchemy")
    assert classify_call("User.objects.filter", "filter") == ("db_method_call", "filter", "django")
    assert classify_call("Order.findOne", "findOne") == ("db_method_call", "findOne", "orm")


def test_classify_none() -> None:
    assert classify_call("logger.info", "info") is None
    assert classify_call("foo.bar", "bar") is None


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
