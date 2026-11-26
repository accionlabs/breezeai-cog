"""A Python ``session`` is an HTTP client or a SQLAlchemy session only where the file shows it
(python/clients.py)."""

from __future__ import annotations

SQLA = ("db_method_call", "delete", "sqlalchemy")
HTTP = ("api_call", "DELETE", None)


def _calls(stmts, verb: str) -> dict[str, tuple[str | None, str | None, str | None]]:
    """Statement text → (semanticType, method, dataAccessHint) for calls of ``.verb(``."""
    return {
        s.text.strip(): (s.semanticType, s.method, s.dataAccessHint)
        for s in stmts
        if f".{verb}(" in s.text and s.semanticType
    }


def test_sqlalchemy_sessions_the_file_shows_are_data_access(analyze_files) -> None:
    calls = _calls(
        analyze_files(
            {
                "flask.py": (
                    "from flask_sqlalchemy import SQLAlchemy\n"
                    "db = SQLAlchemy()\n"
                    "def remove(order):\n"
                    "    db.session.delete(order)\n"
                ),
                "typed.py": (
                    "import requests\n"
                    "from sqlalchemy.orm import Session\n"
                    "def remove(session: Session, order):\n"
                    "    session.delete(order)\n"
                ),
                "with.py": (
                    "from sqlalchemy.orm import Session\n"
                    "def remove(order):\n"
                    "    with Session(engine) as s:\n"
                    "        s.delete(order)\n"
                ),
                "maker.py": (
                    "from sqlalchemy.orm import sessionmaker\n"
                    "SessionLocal = sessionmaker(bind=engine)\n"
                    "def remove(order):\n"
                    "    with SessionLocal() as db_sess:\n"
                    "        db_sess.delete(order)\n"
                ),
                "model.py": (
                    "from sqlmodel import Session\n"
                    "class Repo:\n"
                    "    def __init__(self, session: Session):\n"
                    "        self.session = session\n"
                    "    def remove(self, order):\n"
                    "        self.session.delete(order)\n"
                ),
            }
        ),
        "delete",
    )
    for text in (
        "db.session.delete(order)",
        "session.delete(order)",
        "s.delete(order)",
        "db_sess.delete(order)",
        "self.session.delete(order)",
    ):
        assert calls[text] == SQLA, text


def test_flask_sqlalchemy_session_is_data_access_beside_requests(analyze_files) -> None:
    # db.session is the Flask-SQLAlchemy session, even in a file that imports requests and not
    # the ORM (``db`` comes from the app package); an untyped session there is neither.
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
    assert calls["db.session.delete(order)"] == SQLA
    assert "session.delete(url)" not in calls


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
        assert calls[text] == HTTP, text


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
    assert calls["session.delete(order)"] == SQLA
    assert calls["client.delete(url)"] == HTTP
    # an untyped session in a file that imports both is either: unrecorded
    assert "sess.delete(url)" not in calls


def test_a_name_bound_to_a_client_and_to_something_else_is_neither(analyze_files) -> None:
    calls = _calls(
        analyze_files(
            {
                "m.py": (
                    "import requests\n"
                    "from sqlalchemy.orm import Session\n"
                    "def a(url):\n"
                    "    session = requests.Session()\n"
                    "    session.delete(url)\n"
                    "def b(order):\n"
                    "    session = Session(engine)\n"
                    "    session.delete(order)\n"
                )
            }
        ),
        "delete",
    )
    assert calls == {}


def test_sessions_of_unlisted_libraries_are_unrecorded(analyze_files) -> None:
    # A session the file does not show is SQLAlchemy's or a listed HTTP library's is left
    # unrecorded, whatever it is: an unlisted HTTP library, another ORM, a database driver.
    calls = _calls(
        analyze_files(
            {
                "niquests_named.py": (
                    "import niquests\n"
                    "def f(url):\n"
                    "    session = niquests.Session()\n"
                    "    session.delete(url)\n"
                ),
                "niquests_param.py": (
                    "import niquests\ndef f(session, url):\n    session.delete(url)\n"
                ),
                "niquests_beside_orm.py": (
                    "import niquests\n"
                    "from sqlalchemy import select\n"
                    "def f(url):\n"
                    "    session = niquests.Session()\n"
                    "    session.delete(url)\n"
                ),
                "pony.py": (
                    "from pony.orm import db_session\n"
                    "def f(session, obj):\n"
                    "    session.delete(obj)\n"
                ),
                "tortoise.py": (
                    "import requests\n"
                    "from tortoise import Tortoise\n"
                    "def f(session, obj):\n"
                    "    session.delete(obj)\n"
                ),
                "cassandra.py": (
                    "from cassandra.cluster import Cluster\n"
                    "def f(obj):\n"
                    "    session = Cluster().connect()\n"
                    "    session.delete(obj)\n"
                ),
                "untyped.py": (
                    "from sqlalchemy import select\n"
                    "def remove(session, order):\n"
                    "    session.delete(order)\n"
                ),
                "sqla_beside_unlisted_http.py": (
                    "import niquests\n"
                    "from sqlalchemy import select\n"
                    "def f(session, url):\n"
                    "    session.delete(url)\n"
                ),
                "request_session.py": (
                    "from sqlalchemy.orm import Session\n"
                    "def a(session: Session, o):\n"
                    "    session.query(o)\n"
                    "def b(request):\n"
                    "    request.session.delete()\n"
                ),
                "app_factory.py": (
                    "import requests\n"
                    "from app.db import get_session\n"
                    "def f(obj):\n"
                    "    session = get_session()\n"
                    "    session.delete(obj)\n"
                ),
            }
        ),
        "delete",
    )
    assert calls == {}


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
