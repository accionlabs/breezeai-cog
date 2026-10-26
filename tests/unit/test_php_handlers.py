"""PHP entry points lead to their handler's declaration (contract §5, ``php/handlers.py``)."""

from __future__ import annotations

import pytest

from breezeai_cog.parsers.contract import is_entry_point

ROUTES_HEAD = "<?php\nuse Illuminate\\Support\\Facades\\Route;\n"
USER_CONTROLLER = (
    "<?php\nnamespace App\\Http\\Controllers;\n\n"
    "class UserController\n{\n    public function index() {}\n\n    public function show($id) {}\n}\n"
)


def _routes(statements):
    return {s.endpoint: s for s in statements if is_entry_point(s)}


def test_imported_controller_method_is_located(analyze_files) -> None:
    routes = _routes(analyze_files({
        "routes/web.php": ROUTES_HEAD + "use App\\Http\\Controllers\\UserController;\n"
        "Route::get('/users', [UserController::class, 'index']);\n",
        "app/Http/Controllers/UserController.php": USER_CONTROLLER,
    }))
    r = routes["/users"]
    assert (r.handler, r.handlerPath, r.handlerLine) == (
        "index", "app/Http/Controllers/UserController.php", 6)


def test_unrelated_same_file_function_is_not_the_handler(analyze_files) -> None:
    routes = _routes(analyze_files({
        "routes/web.php": ROUTES_HEAD + "Route::get('/dash', 'DashController@index');\nfunction index() {}\n",
    }))
    r = routes["/dash"]
    assert (r.handler, r.handlerLine, r.handlerPath) == ("DashController@index", None, None)


def test_short_name_declared_twice_is_not_located(analyze_files) -> None:
    routes = _routes(analyze_files({
        "routes/web.php": ROUTES_HEAD + "Route::get('/u', 'UserController@index');\n",
        "app/Http/Controllers/UserController.php": USER_CONTROLLER,
        "app/Http/Controllers/Admin/UserController.php": USER_CONTROLLER.replace(
            "Controllers;", "Controllers\\Admin;"),
    }))
    assert (routes["/u"].handler, routes["/u"].handlerLine) == ("UserController@index", None)


def test_import_picks_one_of_two_same_named_classes(analyze_files) -> None:
    routes = _routes(analyze_files({
        "routes/web.php": ROUTES_HEAD + "use App\\Http\\Controllers\\Admin\\UserController;\n"
        "Route::get('/u', [UserController::class, 'show']);\n",
        "app/Http/Controllers/UserController.php": USER_CONTROLLER,
        "app/Http/Controllers/Admin/UserController.php": USER_CONTROLLER.replace(
            "Controllers;", "Controllers\\Admin;"),
    }))
    r = routes["/u"]
    assert (r.handler, r.handlerPath, r.handlerLine) == (
        "show", "app/Http/Controllers/Admin/UserController.php", 8)


def test_vendor_class_is_not_matched_by_short_name(analyze_files) -> None:
    routes = _routes(analyze_files({
        "routes/web.php": ROUTES_HEAD + "Route::get('/v', 'Vendor\\Pkg\\UserController@index');\n",
        "app/Http/Controllers/UserController.php": USER_CONTROLLER,
    }))
    assert (routes["/v"].handlerLine, routes["/v"].handlerPath) == (None, None)


def test_invokable_controller_is_located(analyze_files) -> None:
    routes = _routes(analyze_files({
        "routes/web.php": ROUTES_HEAD + "use App\\Http\\Controllers\\Ping;\n"
        "Route::get('/ping', Ping::class);\n",
        "app/Http/Controllers/Ping.php": "<?php\nnamespace App\\Http\\Controllers;\n"
        "class Ping\n{\n    public function __invoke() {}\n}\n",
    }))
    r = routes["/ping"]
    assert (r.handler, r.handlerPath, r.handlerLine) == ("__invoke", "app/Http/Controllers/Ping.php", 5)


@pytest.mark.parametrize("jobs", [1, 2])
def test_located_the_same_for_any_jobs(tmp_path, jobs) -> None:
    import gzip
    import json

    from breezeai_cog import analyze_repo

    repo = tmp_path / "repo"
    files = {
        "routes/web.php": ROUTES_HEAD + "use App\\Http\\Controllers\\UserController;\n"
        "Route::get('/users', [UserController::class, 'index']);\n",
        "app/Http/Controllers/UserController.php": USER_CONTROLLER,
    }
    for rel, src in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(src)
    result = analyze_repo(repo, capture_statements=True, out=tmp_path / "out", jobs=jobs)
    lines = gzip.open(result.out_path, "rt", encoding="utf-8").read().splitlines()
    rec = next(json.loads(line) for line in lines if '"routes/web.php"' in line)
    (route,) = [s for s in rec["statements"] if s.get("semanticType") == "route"]
    assert (route["handler"], route["handlerLine"], route["handlerPath"]) == (
        "index", 6, "app/Http/Controllers/UserController.php")
