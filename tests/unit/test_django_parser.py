"""Django parser: selection, gates, look-alikes, framework label, cross-file wiring for any
``--jobs``, and schema validity."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.schemas import FileRecord

URLS = (
    "from django.urls import path\n"
    "def home(request):\n    pass\n"
    "urlpatterns = [path('', home)]\n"
)


def _parse(tmp_path: Path, source: str, *, path: str = "urls.py", capture: bool = True) -> FileRecord:
    p = tmp_path / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(source)
    registry.discover_builtin()
    parser = registry.select(path, source.encode())
    assert parser is not None
    ctx = ParseContext(path=path, abs_path=p, source=source.encode(), repo_root=tmp_path,
                       capture_statements=capture)
    return parser.parse_file(ctx)


def _routes(rec: FileRecord) -> list:
    return [s for s in rec.statements if s.semanticType == "route"]


def test_claims_selects_django_but_fastapi_keeps_its_files() -> None:
    registry.discover_builtin()
    assert registry.select("urls.py", URLS.encode()).name == "python-django"
    both = b"from fastapi import FastAPI\nfrom django.db import models\n"
    assert registry.select("main.py", both).name == "python-fastapi"


def test_route_without_an_index_has_no_address(tmp_path) -> None:
    rec = _parse(tmp_path, URLS)
    (route,) = _routes(rec)
    assert (route.method, route.endpoint, route.handler, route.handlerLine) == ("ANY", None, "home", 2)
    assert (route.nodeType, route.parentId) == ("call", "urls.py")
    assert rec.framework == "django"


def test_no_routes_without_statement_capture(tmp_path) -> None:
    rec = _parse(tmp_path, URLS, capture=False)
    assert _routes(rec) == [] and rec.framework == "django"


def test_fixture_files_have_no_routes(tmp_path) -> None:
    assert _routes(_parse(tmp_path, URLS, path="urls.test.py")) == []


def test_a_path_helper_that_is_not_djangos_is_not_a_route(tmp_path) -> None:
    rec = _parse(tmp_path, (
        "from django.conf import settings\nfrom myapp.routing import path\n"
        "def home(request):\n    pass\nurlpatterns = [path('', home)]\n"
    ))
    assert _routes(rec) == []


def test_a_views_module_is_labelled_without_routes(tmp_path) -> None:
    rec = _parse(tmp_path, "from django.shortcuts import render\ndef home(request):\n    return render(request, 'x')\n",
                 path="views.py")
    assert rec.framework == "django" and _routes(rec) == []


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, URLS)
    schema = Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
    assert not list(schema.iter_errors(json.loads(to_line(rec))))


@pytest.mark.parametrize("jobs", [1, 2])
def test_include_chain_and_views_across_files_for_any_jobs(tmp_path, jobs) -> None:
    from breezeai_cog import analyze_repo

    repo = tmp_path / "repo"
    files = {
        "project/settings.py": "ROOT_URLCONF = 'project.urls'\nMIDDLEWARE = ['x.Auth']\n",
        "project/urls.py": "from django.urls import path, include\nurlpatterns = [path('shop/', include('shop.urls'))]\n",
        "shop/__init__.py": "",
        "shop/urls.py": "from django.urls import path\nfrom . import views\nurlpatterns = [path('orders/', views.Orders.as_view())]\n",
        "shop/views.py": "from django.views import View\nclass Orders(View):\n    def get(self, request):\n        pass\n",
    }
    for rel, src in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(src)
    result = analyze_repo(repo, capture_statements=True, out=tmp_path / "out", jobs=jobs)
    lines = gzip.open(result.out_path, "rt", encoding="utf-8").read().splitlines()
    rec = next(json.loads(line) for line in lines if '"shop/urls.py"' in line)
    (route,) = [s for s in rec["statements"] if s.get("semanticType") == "route"]
    assert (route["method"], route["endpoint"], route["handler"], route["handlerLine"],
            route["handlerPath"], route["guards"]) == ("GET", "/shop/orders/", "get", 3, "shop/views.py", ["x.Auth"])
