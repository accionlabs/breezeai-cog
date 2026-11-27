"""Python HTTP calls made through the application's own helpers are recorded where they run
(BREEZEAI-1654, ``parsers/python_http_helpers.py``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog import iter_file_records

RETRY = '''import aiohttp
import requests


async def make_request(session: aiohttp.ClientSession, url: str, method: str = "GET", retries: int = 3, **kw):
    if method.upper() == "POST":
        response = await session.post(url, **kw)
    else:
        response = await session.get(url, **kw)
    return response


def fetch_json(url):
    return requests.get(url, timeout=10).json()
'''


@pytest.fixture
def api_calls(tmp_path: Path):
    def run(files: dict[str, str]) -> list[tuple[str, int, str | None, str | None]]:
        repo = tmp_path / "repo"
        for rel, src in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src)
        return sorted((r.path.rsplit("/", 1)[-1], s.startLine, s.method, s.endpoint)
                      for r in iter_file_records(repo) for s in r.statements
                      if s.semanticType == "api_call")

    return run


def test_keyword_and_default_verb(api_calls) -> None:
    got = api_calls({"util/__init__.py": "", "util/http.py": RETRY, "app.py": '''from util.http import make_request, fetch_json


async def run(session, doc_id):
    await make_request(session, url=f"https://docs.acme.com/{doc_id}", method="POST")
    await make_request(session, "https://docs.acme.com/list")
    fetch_json("https://api.acme.com/v1/items")
'''})
    assert got == [
        ("app.py", 5, "POST", "https://docs.acme.com/{doc_id}"),
        ("app.py", 6, "GET", "https://docs.acme.com/list"),  # the verb parameter's default
        ("app.py", 7, "GET", "https://api.acme.com/v1/items"),
    ]


def test_helper_with_no_caller_has_no_record(api_calls) -> None:
    assert api_calls({"util/http.py": RETRY}) == []


def test_method_relay_in_a_class(api_calls) -> None:
    got = api_calls({"client.py": '''import requests


class Client:
    def __init__(self, base):
        self.base = base

    def request(self, method, path):
        return requests.request(method, self.base + path)

    def get(self, path):
        return self.request("GET", path)

    def health(self):
        return self.get("/health")
''', "use.py": '''from client import Client


def run(c):
    return Client.get(c, "/orders")
'''})
    # a same-file call through the relay; a call through the class name passes the instance
    # first, so its positions are unsure: no record
    assert got == [("client.py", 15, "GET", "${base}/health")]


def test_function_naming_its_own_endpoint_is_no_helper(api_calls) -> None:
    got = api_calls({"orders.py": '''import requests


def load(base):
    return requests.get(f"{base}/orders")


def run():
    load("http://svc")
'''})
    assert got == [("orders.py", 5, "GET", "${base}/orders")]


def test_local_value_and_unproven(api_calls) -> None:
    got = api_calls({"http.py": '''import requests


def get(url):
    return requests.get(url)


def two(host, path):
    return requests.get(host + path)
''', "use.py": '''from http import get, two


def run():
    url = "https://api.acme.com/health"
    get(url)
    two("h", "/p")
'''})
    assert ("use.py", 6, "GET", "https://api.acme.com/health") in got
    assert ("http.py", 9, "GET", "${host}${path}") in got


def test_same_name_on_another_object_is_not_the_helper(api_calls) -> None:
    got = api_calls({"scraper.py": '''import requests


class Scraper:
    def get(self, url):
        return requests.get(url)

    def execute(self, config):
        kind = config.get("request", "")
        return self.get("https://site.example/" + kind)
'''})
    # `config.get("request")` reads a dict; `self.get(…)` is the helper
    assert got == [("scraper.py", 10, "GET", "https://site.example/{kind}")]


def test_a_keyword_splat_may_carry_the_verb(api_calls) -> None:
    got = api_calls({"util/__init__.py": "", "util/http.py": RETRY, "app.py": '''from util.http import make_request


async def run(session, opts):
    await make_request(session, "https://docs.acme.com/a", **opts)
    await make_request(session, "https://docs.acme.com/b", method="POST", **opts)
'''})
    # no `method` written: the default is unsure, `**opts` may pass one
    assert got == [("app.py", 5, None, "https://docs.acme.com/a"),
                   ("app.py", 6, "POST", "https://docs.acme.com/b")]
