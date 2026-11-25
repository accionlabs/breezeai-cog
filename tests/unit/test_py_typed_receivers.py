"""Python calls on a receiver holding a repository class (``parsers/python/receivers.py``,
``call_targets.py``): ``repo: OrderRepo`` / ``repo = OrderRepo()`` / ``self.repo = OrderRepo()``
then ``repo.save()`` → the file declaring ``save`` in ``OrderRepo``'s resolution order. Only when
every binding names that one class; otherwise no new path."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog import iter_file_records

REPO = {
    "store/__init__.py": "from .repo import OrderRepo\n",
    "store/repo.py": ("class BaseRepo:\n    def save(self, o):\n        return o\n\n"
                      "class OrderRepo(BaseRepo):\n    def find(self, i):\n        return i\n\n"
                      "class OtherRepo:\n    def find(self, i):\n        return i\n"),
}
SAVE, FIND = ("save", "store/repo.py"), ("find", "store/repo.py")


@pytest.fixture
def calls_of(tmp_path: Path):
    """``{path: source}`` → ``{"Class.method" | "function": [(name, path), …]}``."""

    def run(files: dict[str, str]) -> dict[str, list[tuple[str, str | None]]]:
        repo = tmp_path / "repo"
        for rel, src in {**REPO, **files}.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src)
        out: dict[str, list[tuple[str, str | None]]] = {}
        for rec in iter_file_records(repo):
            for fn in rec.functions:
                parts = fn.id.split("#")
                out[f"{parts[1]}.{fn.name}" if len(parts) == 3 else fn.name] = \
                    [(c.name, c.path) for c in fn.calls]
        return out

    return run


def test_annotated_parameter_and_constructed_local(calls_of) -> None:
    got = calls_of({"app.py": (
        "from typing import Optional\nfrom store import OrderRepo\nfrom store import repo as sr\n\n"
        "def a(repo: OrderRepo):\n    repo.find(1)\n    return repo.save(2)\n\n"
        "def b():\n    r = OrderRepo()\n    return r.save(1)\n\n"
        "def c(r: Optional['OrderRepo'] = None):\n    return r.find(1)\n\n"
        "def d(r: 'OrderRepo | None'):\n    return r.find(1)\n\n"
        "def e():\n    r = sr.OrderRepo()\n    return r.find(1)\n")})
    assert FIND in got["a"] and SAVE in got["a"]       # `save` is the base class's
    assert SAVE in got["b"]
    assert FIND in got["c"] and FIND in got["d"] and FIND in got["e"]


def test_attribute_set_in_init(calls_of) -> None:
    got = calls_of({"svc.py": (
        "from store import OrderRepo\n\n"
        "class Service:\n"
        "    cache = None\n"
        "    def __init__(self, repo: OrderRepo):\n        self.repo = repo\n        self.other = OrderRepo()\n"
        "        self.lazy = None\n"
        "    def start(self):\n        self.lazy = OrderRepo()\n"
        "    def run(self):\n"
        "        self.repo.find(1)\n        self.other.save(2)\n        return self.lazy.find(3)\n")})
    assert got["Service.run"].count(FIND) == 1 and SAVE in got["Service.run"]


def test_class_level_annotation(calls_of) -> None:
    got = calls_of({"m.py": (
        "from dataclasses import dataclass\nfrom store import OrderRepo\n\n"
        "@dataclass\nclass Job:\n    repo: OrderRepo\n"
        "    def go(self):\n        return self.repo.find(1)\n")})
    assert FIND in got["Job.go"]


# ---- no new path: a binding that names no class, or two classes ----

def test_rebound_or_untyped_local_has_no_type(calls_of) -> None:
    got = calls_of({"app.py": (
        "from store.repo import OrderRepo, OtherRepo\n\ndef make():\n    return OrderRepo()\n\n"
        "def a(flag):\n    r = OrderRepo()\n    if flag:\n        r = OtherRepo()\n    return r.find(1)\n\n"
        "def b():\n    r = make()\n    return r.find(1)\n\n"
        "def c(items):\n    r = OrderRepo()\n    for r in items:\n        r.find(1)\n\n"
        "def d(rs):\n    r = OrderRepo()\n    return [r.find(1) for r in rs]\n\n"
        "def e(r: OrderRepo):\n    with open('x') as r:\n        return r.find(1)\n\n"
        "def f(r: OrderRepo, *rest):\n    r, q = rest\n    return r.find(1)\n\n"
        "def g(r: OrderRepo):\n    fn = lambda r: r.find(1)\n    return fn\n\n"
        "def h(r: list[OrderRepo]):\n    return r.find(1)\n\n"
        "def i(r: OrderRepo | OtherRepo):\n    return r.find(1)\n\n"
        "def j(r: type[OrderRepo]):\n    return r.find(1)\n")})
    for fn in "abcdefghij":
        assert FIND not in got[fn], fn


def test_attribute_with_two_classes_or_unknown_value(calls_of) -> None:
    got = calls_of({"svc.py": (
        "from store.repo import OrderRepo, OtherRepo\n\ndef make():\n    return OrderRepo()\n\n"
        "class A:\n    def __init__(self):\n        self.r = OrderRepo()\n"
        "    def swap(self):\n        self.r = OtherRepo()\n"
        "    def run(self):\n        return self.r.find(1)\n\n"
        "class B:\n    def __init__(self):\n        self.r = make()\n"
        "    def run(self):\n        return self.r.find(1)\n\n"
        "class C:\n    def __init__(self, r):\n        self.r = r\n"
        "    def run(self):\n        return self.r.find(1)\n\n"
        "class D:\n    def __init__(self, r: OrderRepo):\n        r = r or OtherRepo()\n        self.r = r\n"
        "    def run(self):\n        return self.r.find(1)\n\n"
        "class E:\n    def __init__(self):\n        self.r, self.s = OrderRepo(), 1\n"
        "    def run(self):\n        return self.r.find(1)\n")})
    for cls in "ABCDE":
        assert FIND not in got[f"{cls}.run"], cls


def test_subclass_rebinding_the_attribute_spoils_it(calls_of) -> None:
    # an instance of `Fast` runs `Base.run` with an `OtherRepo`: unsure which `find`
    got = calls_of({"base.py": (
        "from store.repo import OrderRepo\n\nclass Base:\n"
        "    def __init__(self):\n        self.r = OrderRepo()\n"
        "    def run(self):\n        return self.r.find(1)\n"),
        "fast.py": ("from store.repo import OtherRepo\nfrom base import Base\n\nclass Fast(Base):\n"
                    "    def __init__(self):\n        self.r = OtherRepo()\n")})
    assert ("find", None) in got["Base.run"]


def test_inherited_attribute_resolves(calls_of) -> None:
    got = calls_of({"base.py": (
        "from store.repo import OrderRepo\n\nclass Base:\n"
        "    def __init__(self):\n        self.r = OrderRepo()\n"),
        "child.py": ("from base import Base\n\nclass Child(Base):\n"
                     "    def run(self):\n        return self.r.find(1)\n")})
    assert FIND in got["Child.run"]


def test_attribute_named_like_a_method_or_library_class(calls_of) -> None:
    got = calls_of({"svc.py": (
        "import requests\nfrom store.repo import OrderRepo\n\nclass A:\n"
        "    def __init__(self):\n        self.r = OrderRepo()\n        self.http = requests.Session()\n"
        "    def r(self):\n        return None\n"
        "    def run(self):\n        self.http.get('u')\n        return self.r.find(1)\n")})
    assert FIND not in got["A.run"]
    assert ("get", None) in got["A.run"]


def test_library_base_first_gives_no_path(calls_of) -> None:
    got = calls_of({"v.py": (
        "from django.views import View\nfrom store.repo import OrderRepo\n\n"
        "class Page(View, OrderRepo):\n    pass\n\n"
        "def a(p: Page):\n    return p.find(1)\n")})
    assert ("find", None) in got["a"]


def test_attribute_bound_by_with_for_or_a_nested_function(calls_of) -> None:
    got = calls_of({"svc.py": (
        "from store.repo import OrderRepo\n\n"
        "class A:\n    def __init__(self):\n        self.r = OrderRepo()\n"
        "    def open(self, f):\n        with f() as self.r:\n            pass\n"
        "    def run(self):\n        return self.r.find(1)\n\n"
        "class B:\n    def __init__(self, rs):\n        self.r = OrderRepo()\n"
        "        for self.r in rs:\n            pass\n"
        "    def run(self):\n        return self.r.find(1)\n\n"
        "class C:\n    def __init__(self, r: OrderRepo):\n        self.r = OrderRepo()\n"
        "        def later(r):\n            self.r = r\n        self.later = later\n"
        "    def run(self):\n        return self.r.find(1)\n")})
    for cls in "ABC":
        assert FIND not in got[f"{cls}.run"], cls
