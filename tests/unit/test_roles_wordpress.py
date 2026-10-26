"""Role table: docs/entry-point-roles/wordpress.md — one test per row."""

from __future__ import annotations


def _hooks(analyze_files, body: str):
    return [s for s in analyze_files({"wp-content/plugins/p/p.php": "<?php\n" + body})
            if s.semanticType == "eventbus_consumer"]


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
