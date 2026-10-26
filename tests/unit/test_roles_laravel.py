"""Role table: docs/entry-point-roles/laravel.md — one test per row."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

HEAD = "<?php\nuse Illuminate\\Support\\Facades\\Route;\n"
USERS = (
    "<?php\nnamespace App\\Http\\Controllers;\n\nclass UserController\n{\n"
    "    public function index() {}\n}\n"
)


def _eps(analyze_files, routes: str, **extra: str):
    files = {"routes/web.php": HEAD + routes, **extra}
    return [s for s in analyze_files(files) if is_entry_point(s)]


def test_verb_call_is_binding(analyze_files) -> None:
    (r,) = _eps(analyze_files, "use App\\Http\\Controllers\\UserController;\n"
                "Route::get('/users', [UserController::class, 'index']);\n",
                **{"app/Http/Controllers/UserController.php": USERS})
    assert (r.method, r.endpoint, r.routeKind, r.framework) == ("GET", "/users", "route", "laravel")
    assert (r.handler, r.handlerPath, r.handlerLine) == (
        "index", "app/Http/Controllers/UserController.php", 6)


def test_any_is_any(analyze_files) -> None:
    (r,) = _eps(analyze_files, "Route::any('/hook', 'Hook@handle');\n")
    assert (r.method, r.handler) == ("ANY", "Hook@handle")


def test_match_is_one_binding_per_verb(analyze_files) -> None:
    eps = _eps(analyze_files, "Route::match(['get', 'post'], '/form', 'Form@show');\n")
    assert sorted((r.method, r.endpoint) for r in eps) == [("GET", "/form"), ("POST", "/form")]


def test_closure_handler_is_anonymous(analyze_files) -> None:
    (r,) = _eps(analyze_files, "Route::get('/c', function () {\n    return 1;\n});\n")
    assert (r.handler, r.handlerLine) == (None, 3)


def test_dynamic_path_has_empty_endpoint(analyze_files) -> None:
    (r,) = _eps(analyze_files, "Route::get($uri, 'X@y');\n")
    assert (r.method, r.endpoint) == ("GET", None)


def test_view_and_redirect_have_no_handler(analyze_files) -> None:
    eps = _eps(analyze_files, "Route::view('/welcome', 'welcome');\nRoute::redirect('/old', '/new');\n")
    assert sorted((r.method, r.endpoint, r.handler) for r in eps) == [
        ("GET", "/old", None), ("GET", "/welcome", None)]


def test_fallback_is_any_without_endpoint(analyze_files) -> None:
    (r,) = _eps(analyze_files, "Route::fallback('Missing@show');\n")
    assert (r.method, r.endpoint, r.handler) == ("ANY", None, "Missing@show")


def test_resource_is_one_binding_per_action(analyze_files) -> None:
    eps = _eps(analyze_files, "Route::resource('photos', P::class)->except(['create', 'edit']);\n")
    assert sorted((r.method, r.endpoint, r.handler) for r in eps) == [
        ("DELETE", "/photos/{photo}", "P@destroy"), ("GET", "/photos", "P@index"),
        ("GET", "/photos/{photo}", "P@show"), ("PATCH", "/photos/{photo}", "P@update"),
        ("POST", "/photos", "P@store"), ("PUT", "/photos/{photo}", "P@update")]
    assert not _eps(analyze_files, "Route::resource('photos', P::class)->only($which);\n")


def test_singleton_registers_show_edit_update(analyze_files) -> None:
    eps = _eps(analyze_files, "Route::apiSingleton('profile', P::class);\n")
    assert sorted((r.method, r.endpoint) for r in eps) == [
        ("GET", "/profile"), ("PATCH", "/profile"), ("PUT", "/profile")]


def test_group_prefix_is_wiring(analyze_files) -> None:
    eps = _eps(analyze_files,
               "Route::prefix('api')->group(function () { Route::get('/a', 'A@a'); });\n"
               "Route::group(['prefix' => $v], function () { Route::get('/b', 'B@b'); });\n")
    assert sorted((r.endpoint or "", r.handler) for r in eps) == [("", "B@b"), ("/api/a", "A@a")]


def test_middleware_is_guard(analyze_files) -> None:
    (r,) = _eps(analyze_files,
                "Route::middleware('auth')->group(function () {\n"
                "    Route::middleware('verified')->get('/a', 'A@a')->middleware('throttle');\n});\n")
    assert r.guards == ["auth", "verified", "throttle"]


EVENTS_HEAD = "<?php\nuse Illuminate\\Support\\Facades\\Event;\nuse Illuminate\\Support\\Facades\\Bus;\n"


def _events(analyze_files, body: str):
    return [s for s in analyze_files({"app/Providers/E.php": EVENTS_HEAD + body})
            if (s.semanticType or "").startswith("eventbus_")]


def test_listen_is_consumer_binding(analyze_files) -> None:
    (r,) = _events(analyze_files, "Event::listen('order.shipped', SendMail::class);\n")
    assert (r.semanticType, r.method, r.endpoint, r.handler) == (
        "eventbus_consumer", "CONSUMER", "order.shipped", "SendMail@handle")


def test_dispatch_is_outbound(analyze_files) -> None:
    got = _events(analyze_files, "Event::dispatch('order.shipped');\nBus::dispatch(new Job());\n")
    assert sorted((r.semanticType, r.endpoint or "", r.method) for r in got) == [
        ("eventbus_publish", "order.shipped", None), ("eventbus_send", "", None)]


def test_subscribe_is_wiring(analyze_files) -> None:
    assert _events(analyze_files, "Event::subscribe(UserSubscriber::class);\n") == []
