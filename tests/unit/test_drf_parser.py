"""DRF parser and the ``drf-routers`` detector: selection, labels, look-alikes, and routers
registered in another file for any ``--jobs``."""

from __future__ import annotations

import gzip
import json

import pytest

from breezeai_cog.core import registry
from breezeai_cog.parsers.contract import is_entry_point


def test_drf_wins_a_file_that_imports_django_and_drf() -> None:
    registry.discover_builtin()
    both = b"from django.urls import path\nfrom rest_framework import routers\n"
    assert registry.select("urls.py", both).name == "python-drf"
    assert registry.select("urls.py", b"from django.urls import path\n").name == "python-django"


def test_framework_labels(tmp_path) -> None:
    from breezeai_cog import iter_file_records

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "serializers.py").write_text("from rest_framework import serializers\n")
    (repo / "models.py").write_text("from django.db import models\n")
    labels = {r.path: r.framework for r in iter_file_records(repo)}
    assert labels == {"serializers.py": "drf", "models.py": "django"}


def test_admin_register_is_not_a_router(analyze_files) -> None:
    st = analyze_files({
        "admin.py": "from django.contrib import admin\nfrom .models import Order\nadmin.site.register(Order)\n",
        "signals.py": "registry = Registry()\nregistry.register('orders', OrderViewSet)\n",
    })
    assert [s for s in st if is_entry_point(s)] == []


@pytest.mark.parametrize("jobs", [1, 2])
def test_router_registered_in_a_file_without_drf_for_any_jobs(tmp_path, jobs) -> None:
    from breezeai_cog import analyze_repo

    repo = tmp_path / "repo"
    files = {
        "project/settings.py": "ROOT_URLCONF = 'project.urls'\n",
        "project/urls.py": (
            "from django.urls import path, include\nfrom api.routers import router\n"
            "urlpatterns = [path('api/', include(router.urls))]\n"),
        "api/__init__.py": "",
        "api/routers.py": "from rest_framework.routers import SimpleRouter\nrouter = SimpleRouter()\n",
        "api/views.py": "from rest_framework import viewsets\nclass OrderViewSet(viewsets.ReadOnlyModelViewSet):\n    pass\n",
        "api/register.py": "from api.routers import router\nfrom api.views import OrderViewSet\n"
                           "router.register('orders', OrderViewSet)\n",
    }
    for rel, src in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(src)
    result = analyze_repo(repo, capture_statements=True, out=tmp_path / "out", jobs=jobs)
    lines = gzip.open(result.out_path, "rt", encoding="utf-8").read().splitlines()
    rec = next(json.loads(line) for line in lines if '"api/register.py"' in line)
    routes = sorted((s["method"], s["endpoint"], s["handler"], s["handlerPath"])
                    for s in rec["statements"] if s.get("semanticType") == "route")
    assert routes == [("GET", "/api/orders/", "list", "api/views.py"),
                      ("GET", "/api/orders/<pk>/", "retrieve", "api/views.py")]
    assert rec["framework"] == "drf"  # set by the detector: the file never imports DRF


ROUTED = {
    "api/__init__.py": "",
    "api/v1/__init__.py": "",
    "api/v1/views.py": "from rest_framework import viewsets\nclass OrderViewSet(viewsets.ModelViewSet):\n    pass\n",
    "api/v1/routers.py": (
        "from rest_framework.routers import DefaultRouter\nfrom .views import OrderViewSet\n"
        "router = DefaultRouter()\nrouter.register('orders', OrderViewSet)\n"),
    "settings.py": "ROOT_URLCONF = 'urls'\n",
    "urls.py": (
        "from django.urls import include, path\nimport api.v1.routers\n"
        "urlpatterns = [path('api/', include(api.v1.routers.router.urls))]\n"),
}


@pytest.mark.parametrize("jobs", [1, 2])
def test_router_included_through_a_dotted_chain_for_any_jobs(capture_routes, jobs) -> None:
    routes = capture_routes(ROUTED, jobs)
    assert sorted((r["method"], r["endpoint"]) for r in routes["api/v1/routers.py"]) == [
        ("DELETE", "/api/orders/<pk>/"), ("GET", "/api/orders/"), ("GET", "/api/orders/<pk>/"),
        ("PATCH", "/api/orders/<pk>/"), ("POST", "/api/orders/"), ("PUT", "/api/orders/<pk>/")]


@pytest.mark.parametrize("jobs", [1, 2])
def test_router_behind_a_chain_that_is_not_in_the_repo_has_no_address(capture_routes, jobs) -> None:
    files = {**ROUTED, "urls.py": ROUTED["urls.py"].replace("api.v1.routers.router", "api.v2.routers.router")}
    routes = capture_routes(files, jobs)
    assert {r.get("endpoint") for r in routes["api/v1/routers.py"]} == {None}
