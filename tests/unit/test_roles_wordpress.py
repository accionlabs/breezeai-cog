"""Role table: docs/entry-point-roles/wordpress.md — one test per row."""

from __future__ import annotations


def _hooks(analyze_files, body: str):
    return [s for s in analyze_files({"wp-content/plugins/p/p.php": "<?php\n" + body})
            if s.semanticType == "eventbus_consumer"]


def _routes(analyze_files, body: str):
    return [s for s in analyze_files({"wp-content/plugins/p/p.php": "<?php\n" + body})
            if s.semanticType == "route"]


def test_rest_route_with_literal_method_and_callback_is_binding(analyze_files) -> None:
    (route,) = _routes(
        analyze_files,
        "class Items {\n"
        "    function register() {\n"
        "        register_rest_route('acme/v1', '/items/(?P<id>\\d+)', [\n"
        "            'methods' => WP_REST_Server::READABLE,\n"
        "            'callback' => [$this, 'get_item'],\n"
        "        ]);\n"
        "    }\n"
        "    function get_item() {}\n"
        "}\n",
    )
    assert (route.method, route.endpoint, route.routeKind, route.framework) == (
        "GET", "/acme/v1/items/(?P<id>\\d+)", "route", "wordpress"
    )
    assert (route.handler, route.handlerLine, route.isRegex) == ("get_item", 9, True)


def test_rest_route_method_arrays_emit_one_binding_per_method(analyze_files) -> None:
    routes = _routes(
        analyze_files,
        "register_rest_route('acme/v1', '/items', [\n"
        "    'methods' => ['GET', 'POST'],\n"
        "    'callback' => 'read_items',\n"
        "]);\n"
        "function read_items() {}\n",
    )
    assert sorted((route.method, route.handler) for route in routes) == [
        ("GET", "read_items"), ("POST", "read_items")
    ]


def test_named_function_callback_is_binding(analyze_files) -> None:
    hooks = _hooks(analyze_files, "add_action('init', 'p_init');\nadd_filter('the_title', 'p_title');\n"
                   "function p_init() {}\nfunction p_title($t) { return $t; }\n")
    assert sorted((h.endpoint, h.method, h.framework, h.handler, h.handlerLine) for h in hooks) == [
        ("init", "CONSUMER", "wordpress", "p_init", 4),
        ("the_title", "CONSUMER", "wordpress", "p_title", 5)]


def test_class_callbacks_name_the_method(analyze_files) -> None:
    hooks = _hooks(analyze_files, "class P {\n    function boot() {\n"
                   "        add_action('init', [$this, 'run']);\n"
                   "        add_action('save', [Other::class, 'save']);\n    }\n"
                   "    function run() {}\n}\n")
    assert sorted((h.endpoint, h.handler, h.handlerLine) for h in hooks) == [
        ("init", "run", 7), ("save", "Other@save", None)]


def test_unknown_object_callback_has_no_handler(analyze_files) -> None:
    (h,) = _hooks(analyze_files, "add_action('init', [$service, 'run']);\nfunction run() {}\n")
    assert (h.handler, h.handlerLine) == (None, None)


def test_closure_callback_is_anonymous(analyze_files) -> None:
    (h,) = _hooks(analyze_files, "add_action('init', function () {\n    return;\n});\n")
    assert (h.handler, h.handlerLine) == (None, 2)


def test_dynamic_hook_has_empty_endpoint(analyze_files) -> None:
    (h,) = _hooks(analyze_files, "add_action($hook, 'p_init');\nfunction p_init() {}\n")
    assert (h.endpoint, h.handler) == (None, "p_init")
