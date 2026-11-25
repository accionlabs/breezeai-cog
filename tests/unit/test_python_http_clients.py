"""A Python ``session`` is an HTTP client only where the file shows it (python/http_clients.py)."""

from __future__ import annotations


def _calls(stmts, verb: str) -> dict[str, tuple[str | None, str | None, str | None]]:
    """Statement text → (semanticType, method, dataAccessHint) for calls of ``.verb(``."""
    return {
        s.text.strip(): (s.semanticType, s.method, s.dataAccessHint)
        for s in stmts
        if f".{verb}(" in s.text and s.semanticType
    }


def test_sqlalchemy_session_delete_is_data_access(analyze_files) -> None:
    stmts = analyze_files(
        {
            "sa.py": (
                "from flask_sqlalchemy import SQLAlchemy\n"
                "db = SQLAlchemy()\n"
                "def remove(order):\n"
                "    db.session.delete(order)\n"
            ),
            "orm.py": (
                "from sqlalchemy.orm import Session\n"
                "def remove(session: Session, order):\n"
                "    session.delete(order)\n"
                "def remove2(order):\n"
                "    with Session(engine) as s:\n"
                "        s.delete(order)\n"
            ),
        }
    )
    calls = _calls(stmts, "delete")
    assert calls["db.session.delete(order)"] == ("db_method_call", "delete", "sqlalchemy")
    assert calls["session.delete(order)"] == ("db_method_call", "delete", "sqlalchemy")
    assert "s.delete(order)" not in calls or calls["s.delete(order)"][0] != "api_call"


def test_flask_sqlalchemy_session_is_data_access_beside_requests(analyze_files) -> None:
    # db.session is the Flask-SQLAlchemy session, even in a file that imports requests and not
    # the ORM (``db`` comes from the app package).
    calls = _calls(
        analyze_files(
            {
                "svc.py": (
                    "import requests\n"
                    "from app import db\n"
                    "def a(order):\n"
                    "    db.session.delete(order)\n"
                    "def b(session, url):\n"
                    "    session.delete(url)\n"
                )
            }
        ),
        "delete",
    )
    assert calls["db.session.delete(order)"] == ("db_method_call", "delete", "sqlalchemy")
    assert calls["session.delete(url)"] == ("api_call", "DELETE", None)


def test_http_sessions_stay_api_calls(analyze_files) -> None:
    calls = _calls(
        analyze_files(
            {
                "http.py": (
                    "import requests as rq\n"
                    "from httpx import AsyncClient\n"
                    "import httpx\n"
                    "class Api:\n"
                    "    def __init__(self):\n"
                    "        self.session = rq.Session()\n"
                    "    def rm(self, url):\n"
                    "        self.session.delete(url)\n"
                    "async def f(url):\n"
                    "    async with AsyncClient() as c:\n"
                    "        await c.delete(url)\n"
                    "def g(url):\n"
                    "    with httpx.Client() as client:\n"
                    "        client.delete(url)\n"
                    "def h(s: rq.Session, url):\n"
                    "    s.delete(url)\n"
                    "def i(url):\n"
                    "    rq.Session().delete(url)\n"
                )
            }
        ),
        "delete",
    )
    for text in (
        "self.session.delete(url)",
        "await c.delete(url)",
        "client.delete(url)",
        "s.delete(url)",
        "rq.Session().delete(url)",
    ):
        assert calls[text] == ("api_call", "DELETE", None), text


def test_orm_and_http_in_one_file_keep_their_own_kind(analyze_files) -> None:
    calls = _calls(
        analyze_files(
            {
                "both.py": (
                    "import requests\n"
                    "from sqlalchemy.orm import Session\n"
                    "def a(session: Session, order):\n"
                    "    session.delete(order)\n"
                    "def b(url):\n"
                    "    client = requests.Session()\n"
                    "    client.delete(url)\n"
                    "def c(sess, url):\n"
                    "    sess.delete(url)\n"
                )
            }
        ),
        "delete",
    )
    assert calls["session.delete(order)"] == ("db_method_call", "delete", "sqlalchemy")
    assert calls["client.delete(url)"] == ("api_call", "DELETE", None)
    # an untyped session in a file that imports the ORM is no HTTP client
    assert calls.get("sess.delete(url)", (None,))[0] != "api_call"


def test_a_name_bound_to_a_client_and_to_something_else_is_no_client(analyze_files) -> None:
    calls = _calls(
        analyze_files(
            {
                "m.py": (
                    "import requests\n"
                    "from sqlalchemy.orm import sessionmaker\n"
                    "def a(url):\n"
                    "    session = requests.Session()\n"
                    "    session.delete(url)\n"
                    "def b(order):\n"
                    "    session = Session()\n"
                    "    session.delete(order)\n"
                )
            }
        ),
        "delete",
    )
    assert all(kind != "api_call" for kind, _, _ in calls.values())


def test_factories_optional_annotations_and_copies_are_clients(analyze_files) -> None:
    # Shapes from real code: a factory typed to return a client, a factory named for an HTTP
    # session in a file that imports no HTTP library, an optional client attribute, a copy.
    stmts = analyze_files(
        {
            "typed.py": (
                "import httpx\n"
                "class Scm:\n"
                "    def _session(self) -> httpx.Client:\n"
                "        return httpx.Client()\n"
                "    def get(self, url):\n"
                "        return self._session().get(url)\n"
            ),
            "named.py": (
                "from app.registry import ConnectionRegistry\n"
                "async def check(url):\n"
                "    session = await ConnectionRegistry.get_http_session('x')\n"
                "    async with session.delete(url) as r:\n"
                "        return r\n"
            ),
            "optional.py": (
                "import aiohttp\n"
                "class Jira:\n"
                "    def __init__(self):\n"
                "        self._session: aiohttp.ClientSession | None = None\n"
                "    async def close(self):\n"
                "        session = self._session\n"
                "        self._session = None\n"
                "    async def _get_session(self) -> aiohttp.ClientSession:\n"
                "        return aiohttp.ClientSession()\n"
                "    async def rm(self, url):\n"
                "        session = await self._get_session()\n"
                "        async with session.delete(url) as r:\n"
                "            return r\n"
            ),
        }
    )
    by_path = {(s.path, s.text.strip()): s for s in stmts if s.semanticType}
    assert by_path[("typed.py", "return self._session().get(url)")].semanticType == "api_call"
    for path in ("named.py", "optional.py"):
        rec = by_path[(path, "session.delete(url)")]
        assert (rec.semanticType, rec.method) == ("api_call", "DELETE"), path
