"""Role table: docs/entry-point-roles/slim.md — one test per row."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

HEAD = "<?php\nuse Slim\\Factory\\AppFactory;\n$app = AppFactory::create();\n"


def _eps(analyze_files, body: str, **extra: str):
    return [s for s in analyze_files({"public/index.php": HEAD + body, **extra}) if is_entry_point(s)]


def test_verb_call_is_binding(analyze_files) -> None:
    (r,) = _eps(analyze_files, "use App\\Action\\Home;\n$app->get('/home', 'App\\Action\\Home:show');\n",
                **{"src/Action/Home.php": "<?php\nnamespace App\\Action;\n\nclass Home\n{\n"
                   "    public function show() {}\n}\n"})
    assert (r.method, r.endpoint, r.routeKind, r.framework) == ("GET", "/home", "route", "slim")
    assert (r.handler, r.handlerPath, r.handlerLine) == ("show", "src/Action/Home.php", 6)


def test_invokable_action_and_any(analyze_files) -> None:
    (r,) = _eps(analyze_files, "$app->any('/ping', Ping::class);\n")
    assert (r.method, r.handler) == ("ANY", "Ping")


def test_map_is_one_binding_per_verb(analyze_files) -> None:
    eps = _eps(analyze_files, "$app->map(['GET', 'POST'], '/form', 'Form:show');\n")
    assert sorted(r.method for r in eps) == ["GET", "POST"]


def test_closure_handler_is_anonymous(analyze_files) -> None:
    (r,) = _eps(analyze_files, "$app->get('/c', function ($rq, $rs) {\n    return $rs;\n});\n")
    assert (r.handler, r.handlerLine) == (None, 4)


def test_group_is_wiring(analyze_files) -> None:
    eps = _eps(analyze_files,
               "$app->group('/api', function ($group) { $group->get('/a', 'A:a'); });\n"
               "$app->group($base, function ($group) { $group->get('/b', 'B:b'); });\n")
    assert sorted((r.endpoint or "", r.handler) for r in eps) == [("", "B@b"), ("/api/a", "A@a")]


def test_add_is_guard(analyze_files) -> None:
    (r,) = _eps(analyze_files,
                "$app->group('/api', function ($group) {\n"
                "    $group->get('/a', 'A:a')->add(new RateLimit());\n})->add(Auth::class);\n")
    assert r.guards == ["Auth", "RateLimit"]


def test_set_name_is_route_name(analyze_files) -> None:
    (r,) = _eps(analyze_files, "$app->get('/a', 'A:a')->setName('home');\n")
    assert r.name == "home"


def test_redirect_is_binding_without_handler(analyze_files) -> None:
    (r,) = _eps(analyze_files, "$app->redirect('/old', '/new');\n")
    assert (r.method, r.endpoint, r.routeKind, r.handler) == ("GET", "/old", "route", None)


def test_startup_is_none(analyze_files) -> None:
    assert _eps(analyze_files, "$app->run();\n") == []
