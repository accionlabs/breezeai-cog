"""Role table: docs/entry-point-roles/codeigniter.md — one test per row."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

HEAD = "<?php\nuse CodeIgniter\\Router\\RouteCollection;\n"
USERS = (
    "<?php\nnamespace App\\Controllers;\n\nclass Users\n{\n    public function show($id) {}\n}\n"
)


def _all(analyze_files, routes: str, **extra: str):
    return analyze_files({"app/Config/Routes.php": HEAD + routes, **extra})


def _eps(analyze_files, routes: str, **extra: str):
    return [s for s in _all(analyze_files, routes, **extra) if is_entry_point(s)]


def test_verb_call_is_binding(analyze_files) -> None:
    (r,) = _eps(analyze_files, "$routes->get('users/(:num)', 'Users::show/$1');\n",
                **{"app/Controllers/Users.php": USERS})
    assert (r.method, r.endpoint, r.routeKind, r.framework) == (
        "GET", "/users/(:num)", "route", "codeigniter")
    assert (r.handler, r.handlerPath, r.handlerLine) == ("show", "app/Controllers/Users.php", 6)


def test_add_and_any_are_any(analyze_files) -> None:
    eps = _eps(analyze_files, "$routes->add('a', 'A::a');\n$routes->any('b', 'B::b');\n")
    assert sorted((r.method, r.endpoint) for r in eps) == [("ANY", "/a"), ("ANY", "/b")]


def test_match_is_one_binding_per_verb(analyze_files) -> None:
    eps = _eps(analyze_files, "$routes->match(['get', 'post'], 'form', 'Form::show');\n")
    assert sorted(r.method for r in eps) == ["GET", "POST"]


def test_placeholders_are_regex(analyze_files) -> None:
    eps = _eps(analyze_files, "$routes->get('a/(:alpha)', 'A::a');\n$routes->get('b', 'B::b');\n")
    assert sorted((r.endpoint, r.isRegex) for r in eps) == [("/a/(:alpha)", True), ("/b", False)]


def test_dynamic_path_has_empty_endpoint(analyze_files) -> None:
    (r,) = _eps(analyze_files, "$routes->get($uri, 'A::a');\n")
    assert (r.method, r.endpoint) == ("GET", None)


def test_closure_handler_is_anonymous(analyze_files) -> None:
    (r,) = _eps(analyze_files, "$routes->get('c', static function () {\n    return 1;\n});\n")
    assert (r.handler, r.handlerLine) == (None, 3)


def test_view_and_redirect_have_no_handler(analyze_files) -> None:
    eps = _eps(analyze_files, "$routes->view('about', 'pages/about');\n$routes->addRedirect('old', 'new');\n")
    assert sorted((r.method, r.endpoint, r.handler) for r in eps) == [
        ("GET", "/about", None), ("GET", "/old", None)]


def test_cli_is_none(analyze_files) -> None:
    assert _eps(analyze_files, "$routes->cli('jobs/run', 'Jobs::run');\n") == []


def test_resource_is_one_binding_per_action(analyze_files) -> None:
    eps = _eps(analyze_files, "$routes->resource('photos', ['only' => ['index', 'show']]);\n")
    assert sorted((r.method, r.endpoint, r.handler) for r in eps) == [
        ("GET", "/photos", "Photos@index"), ("GET", "/photos/(:segment)", "Photos@show")]


def test_map_is_one_binding_per_entry(analyze_files) -> None:
    eps = _eps(analyze_files, "$routes->map(['a' => 'A::a', 'b' => 'B::b']);\n")
    assert sorted((r.method, r.endpoint, r.handler) for r in eps) == [
        ("ANY", "/a", "A@a"), ("ANY", "/b", "B@b")]


def test_group_is_wiring(analyze_files) -> None:
    eps = _eps(analyze_files,
               "$routes->group('admin', ['filter' => 'auth'], static function ($routes) {\n"
               "    $routes->get('users', 'Admin::users');\n});\n"
               "$routes->group($p, static function ($routes) { $routes->get('x', 'X::x'); });\n")
    assert sorted((r.endpoint or "", r.guards) for r in eps) == [
        ("", None), ("/admin/users", ["auth"])]


def test_route_options_are_guard_and_name(analyze_files) -> None:
    (r,) = _eps(analyze_files, "$routes->get('a', 'A::a', ['filter' => 'auth', 'as' => 'home']);\n")
    assert (r.guards, r.name) == (["auth"], "home")


def test_environment_is_wiring(analyze_files) -> None:
    (r,) = _eps(analyze_files,
                "$routes->environment('development', static function ($routes) {\n"
                "    $routes->get('builder', 'Tools::builder');\n});\n")
    assert r.endpoint == "/builder"


def test_events_on_is_consumer_and_trigger_outbound(analyze_files) -> None:
    stmts = _all(analyze_files, "Events::on('pre_system', 'Boot::run');\nEvents::trigger('cache.clear');\n")
    events = sorted((s.semanticType, s.endpoint, s.method)
                    for s in stmts if (s.semanticType or "").startswith("eventbus_"))
    assert events == [("eventbus_consumer", "pre_system", "CONSUMER"),
                      ("eventbus_publish", "cache.clear", None)]
