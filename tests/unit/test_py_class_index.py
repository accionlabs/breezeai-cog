"""Python method calls resolve through the class hierarchy (``parsers/python/call_targets.py``):
``self.m()`` / ``cls.m()`` / ``super().m()`` / ``Class.m()`` → the first class of Python's method
resolution order that declares ``m``; a library base before it → no path."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog import iter_file_records


@pytest.fixture
def calls_of(tmp_path: Path):
    """``{path: source}`` → ``{"Class.method": [(call name, path), …]}``."""

    def run(files: dict[str, str]) -> dict[str, list[tuple[str, str | None]]]:
        repo = tmp_path / "repo"
        for rel, src in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src)
        out: dict[str, list[tuple[str, str | None]]] = {}
        for rec in iter_file_records(repo):
            for fn in rec.functions:
                parts = fn.id.split("#")  # file#Class#method@line, or file#function@line
                key = f"{parts[1]}.{fn.name}" if len(parts) == 3 else fn.name
                out[key] = [(c.name, c.path) for c in fn.calls]
        return out

    return run


BASE = {
    "strategies/__init__.py": "from .base import CrawlStrategy\n",
    "strategies/base.py": ("class CrawlStrategy:\n"
                           "    def download(self, url):\n        return url\n"
                           "    def crawl(self, source):\n        return source\n"),
}


def test_inherited_and_super_calls(calls_of) -> None:
    got = calls_of({**BASE, "sites/site.py": (
        "from strategies import CrawlStrategy\n\n"
        "class SiteCrawl(CrawlStrategy):\n"
        "    def crawl(self, source):\n"
        "        data = super().crawl(source)\n"
        "        self.download(source)\n"
        "        return self.parse(data)\n"
        "    def parse(self, data):\n        return data\n")})
    calls = got["SiteCrawl.crawl"]
    assert ("crawl", "strategies/base.py") in calls      # super(): the parent's, not its own
    assert ("download", "strategies/base.py") in calls   # inherited, through the package re-export
    assert ("parse", "sites/site.py") in calls           # its own method


def test_library_base_first_gives_no_path(calls_of) -> None:
    got = calls_of({**BASE, "views.py": (
        "from django.views import View\nfrom strategies.base import CrawlStrategy\n\n"
        "class Page(View, CrawlStrategy):\n"
        "    def get(self, request):\n"
        "        self.download('x')\n"            # View may declare it: unsure
        "        return self.render(request)\n"   # a library method
        "    def render(self, r):\n        return r\n")})
    calls = got["Page.get"]
    assert ("download", None) in calls
    assert ("render", "views.py") in calls


def test_multiple_inheritance_follows_the_mro(calls_of) -> None:
    got = calls_of({"m.py": (
        "class A:\n    def run(self):\n        return 'a'\n"
        "class B(A):\n    pass\n"
        "class C(A):\n    def run(self):\n        return 'c'\n"
        "class D(B, C):\n    def go(self):\n        return self.run()\n"),
        "other.py": "from m import D\n\ndef use():\n    return D.run(D())\n"})
    # D -> B -> C -> A: C's run, declared in the same file
    assert ("run", "m.py") in got["D.go"]
    assert ("run", "m.py") in got["use"]


def test_class_method_through_the_class_name(calls_of) -> None:
    got = calls_of({**BASE, "factory.py": (
        "from strategies.base import CrawlStrategy\n\n"
        "class Site(CrawlStrategy):\n    pass\n\n"
        "def make():\n    return Site.download(None, 'u')\n")})
    assert ("download", "strategies/base.py") in got["make"]


def test_attribute_holding_a_callable_has_no_path(calls_of) -> None:
    # `self.callback` is set at runtime: no class in the order declares it
    got = calls_of({"h.py": (
        "def callback(x):\n    return x\n\n"
        "class H:\n    def __init__(self, cb):\n        self.callback = cb\n"
        "    def fire(self):\n        return self.callback(1)\n")})
    assert ("callback", None) in got["H.fire"]
