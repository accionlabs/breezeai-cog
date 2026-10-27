"""Backbone.js additive-capture tests."""

from __future__ import annotations

import gzip
import json

import pytest

from breezeai_cog import analyze_repo


def _backbone(analyze_files, source: str):
    return analyze_files({"src/app.js": source})


def test_router_routes_apply_history_root_and_link_handlers(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const TodoRouter = Backbone.Router.extend({\n"
        "  routes: {\n"
        "    '': 'index',\n"
        "    'todos/:id': 'showTodo',\n"
        "  },\n"
        "  index() {},\n"
        "  showTodo() {},\n"
        "});\n"
        "Backbone.history.start({ pushState: true, root: '/app/' });\n",
    )

    routes = [s for s in statements if s.semanticType == "route"]
    assert len(routes) == 2
    assert {(s.endpoint, s.handler, s.framework, s.routeKind) for s in routes} == {
        ("/app", "index", "backbone", "page"),
        ("/app/todos/:id", "showTodo", "backbone", "page"),
    }


def test_router_route_call_is_captured(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const TodoRouter = Backbone.Router.extend({ search() {} });\n"
        "const router = new TodoRouter();\n"
        "router.route('search/:query', 'search');\n",
    )

    [route] = [s for s in statements if s.semanticType == "route"]
    assert route.endpoint == "/search/:query"
    assert route.handler == "search"
    assert route.handlerLine == 1
    assert route.framework == "backbone"
    assert route.routeKind == "page"


def test_non_literal_route_paths_are_not_emitted(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const DYNAMIC_PATH = getItemsPath();\n"
        "const TodoRouter = Backbone.Router.extend({\n"
        "  routes: { '/items': 'items', [DYNAMIC_PATH]: 'dynamic' },\n"
        "});\n"
        "Backbone.history.start({ root: APP_ROOT });\n"
        "const router = new TodoRouter();\n"
        "router.route(DYNAMIC_PATH, 'dynamic');\n",
    )

    routes = [s for s in statements if s.semanticType == "route"]
    assert [(s.endpoint, s.handler) for s in routes] == [("/items", "items")]


def test_regex_router_route_is_captured_as_regex(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const TodoRouter = Backbone.Router.extend({ item() {} });\n"
        "const router = new TodoRouter();\n"
        "Backbone.history.start({ pushState: true, root: '/app/' });\n"
        "router.route(/^items\\/(\\d+)$/i, 'item');\n",
    )

    [route] = [s for s in statements if s.semanticType == "route"]
    assert route.endpoint == r"/^items\/(\d+)$/i"
    assert route.isRegex is True
    assert route.handler == "item"
    assert route.handlerLine == 1


def test_router_routes_resolve_variables_spreads_and_constructor_options(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const DETAIL_PATH = 'items/:id';\n"
        "const sharedRoutes = { '': 'index' };\n"
        "const TodoRouter = Backbone.Router.extend({\n"
        "  routes: { ...sharedRoutes, [DETAIL_PATH]: 'show' },\n"
        "  index() {},\n"
        "  show() {},\n"
        "});\n"
        "new TodoRouter({ routes: { 'settings': 'settings' } });\n",
    )

    routes = [s for s in statements if s.semanticType == "route"]
    assert {(s.endpoint, s.handler) for s in routes} == {
        ("/", "index"),
        ("/items/:id", "show"),
        ("/settings", "settings"),
    }


def test_marionette_app_routes_use_controller_handlers(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const AppRouter = Backbone.Marionette.AppRouter.extend({\n"
        "  appRoutes: { 'about': 'showAbout' },\n"
        "  controller: { showAbout() {} },\n"
        "});\n",
    )

    [route] = [s for s in statements if s.semanticType == "route"]
    assert (route.endpoint, route.handler, route.handlerLine) == ("/about", "showAbout", 3)


def test_router_route_calls_work_through_alias_and_this(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const TodoRouter = Backbone.Router.extend({\n"
        "  localPage() {},\n"
        "  routes: { '': 'home' },\n"
        "  home() {},\n"
        "  registerLocal() { this.route('local', 'localPage'); },\n"
        "});\n"
        "const router = new TodoRouter();\n"
        "const routerAlias = router;\n"
        "routerAlias.route('alias', 'home');\n"
        "router.registerLocal();\n",
    )

    routes = [s for s in statements if s.semanticType == "route"]
    assert {(s.endpoint, s.handler) for s in routes} == {
        ("/", "home"),
        ("/alias", "home"),
        ("/local", "localPage"),
    }


def test_history_root_is_not_applied_when_pushstate_is_disabled(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const TodoRouter = Backbone.Router.extend({ routes: { 'todos': 'list' } });\n"
        "Backbone.history.start({ pushState: false, root: '/app/' });\n",
    )

    [route] = [s for s in statements if s.semanticType == "route"]
    assert route.endpoint == "/todos"


@pytest.mark.parametrize("jobs", [1, 2])
def test_imported_router_route_calls_work_through_analyze_repo(tmp_path, jobs: int) -> None:
    from_path = tmp_path / "repo"
    from_path.mkdir()
    (from_path / "router.js").write_text(
        "import Backbone from 'backbone';\n"
        "export const BaseRouter = Backbone.Router.extend({});\n"
    )
    (from_path / "app.js").write_text(
        "import Backbone from 'backbone';\n"
        "import { BaseRouter } from './router.js';\n"
        "const router = new BaseRouter();\n"
        "router.route('items/:id', 'showItem');\n"
    )

    result = analyze_repo(from_path, out=tmp_path / "output", jobs=jobs)
    assert result.out_path is not None
    with gzip.open(result.out_path, "rt", encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream]
    routes = [
        statement
        for record in records
        for statement in record.get("statements", [])
        if statement.get("semanticType") == "route"
        and statement.get("framework") == "backbone"
    ]
    assert len(routes) == 1
    assert routes[0]["endpoint"] == "/items/:id"
    assert routes[0]["handler"] == "showItem"


def test_model_calls_resolve_urlroot_and_http_methods(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const Todo = Backbone.Model.extend({ urlRoot: '/todos' });\n"
        "const existingTodo = new Todo({ id: 7 });\n"
        "existingTodo.fetch();\n"
        "existingTodo.save();\n"
        "existingTodo.destroy();\n"
        "const newTodo = new Todo();\n"
        "newTodo.save();\n",
    )

    calls = [
        s for s in statements
        if s.semanticType == "api_call" and s.framework == "backbone"
    ]
    assert len(calls) == 4
    assert {(s.method, s.endpoint) for s in calls} == {
        ("GET", "/todos/:id"),
        ("PUT", "/todos/:id"),
        ("DELETE", "/todos/:id"),
        ("POST", "/todos"),
    }
    assert all(s.framework == "backbone" for s in calls)


def test_backbone_sync_maps_operation_to_http_method(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const Todo = Backbone.Model.extend({ urlRoot: '/todos' });\n"
        "const todo = new Todo({ id: 7 });\n"
        "Backbone.sync('read', todo);\n",
    )

    [call] = [
        s for s in statements
        if s.semanticType == "api_call" and s.framework == "backbone"
    ]
    assert (call.method, call.endpoint) == ("GET", "/todos/:id")


def test_collection_calls_resolve_literal_url(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const Todos = Backbone.Collection.extend({ url: '/todos' });\n"
        "const todos = new Todos();\n"
        "todos.fetch();\n"
        "todos.create({ title: 'A todo' });\n",
    )

    calls = [
        s for s in statements
        if s.semanticType == "api_call" and s.framework == "backbone"
    ]
    assert len(calls) == 2
    assert {(s.method, s.endpoint) for s in calls} == {
        ("GET", "/todos"),
        ("POST", "/todos"),
    }


def test_dynamic_model_urlroot_keeps_endpoint_null(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const Todo = Backbone.Model.extend({ urlRoot: API_URL });\n"
        "const todo = new Todo({ id: 7 });\n"
        "todo.fetch();\n",
    )

    [call] = [
        s for s in statements
        if s.semanticType == "api_call" and s.framework == "backbone"
    ]
    assert call.method == "GET"
    assert call.endpoint is None


def test_unrelated_fetch_is_not_tagged_as_backbone(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const frameworkName = 'Backbone';\n"
        "const cache = { fetch() {} };\n"
        "cache.fetch();\n",
    )

    assert not any(
        s.semanticType == "api_call" and s.framework == "backbone"
        for s in statements
    )


def test_backbone_views_are_tagged_as_views(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const TodoView = Backbone.View.extend({ render() {} });\n"
        "class SearchView extends Backbone.View {}\n",
    )

    views = [s for s in statements if s.uiRole == "view"]
    assert len(views) == 2
    assert all(s.framework == "backbone" for s in views)


def test_route_constants_and_computed_route_keys_are_resolved(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const USERS_PATH = 'users/:id';\n"
        "const SEARCH_PATH = 'search';\n"
        "const TodoRouter = Backbone.Router.extend({\n"
        "  routes: { [USERS_PATH]: 'showUser' },\n"
        "  showUser() {},\n"
        "});\n"
        "const router = new TodoRouter();\n"
        "router.route(SEARCH_PATH, 'search');\n",
    )

    routes = [s for s in statements if s.semanticType == "route"]
    assert {(s.endpoint, s.handler) for s in routes} == {
        ("/users/:id", "showUser"),
        ("/search", "search"),
    }


def test_router_routes_getter_is_supported(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const TodoRouter = Backbone.Router.extend({\n"
        "  get routes() { return { 'todos': 'listTodos' }; },\n"
        "  listTodos() {},\n"
        "});\n",
    )

    [route] = [s for s in statements if s.semanticType == "route"]
    assert (route.endpoint, route.handler, route.framework) == (
        "/todos",
        "listTodos",
        "backbone",
    )


def test_derived_models_and_instance_aliases_keep_backbone_endpoint(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const Todo = Backbone.Model.extend({ urlRoot: '/todos' });\n"
        "const SpecializedTodo = Todo.extend({});\n"
        "const todo = new SpecializedTodo({ id: 7 });\n"
        "const todoAlias = todo;\n"
        "todoAlias.fetch();\n",
    )

    [call] = [
        s for s in statements
        if s.semanticType == "api_call" and s.framework == "backbone"
    ]
    assert (call.method, call.endpoint) == ("GET", "/todos/:id")


def test_cross_file_base_model_endpoint_is_inherited(analyze_files) -> None:
    statements = analyze_files({
        "src/base.js": (
            "export const BaseTodo = Backbone.Model.extend({ urlRoot: '/todos' });\n"
        ),
        "src/specialized.js": (
            "import Backbone from 'backbone';\n"
            "import { BaseTodo } from './base';\n"
            "const SpecializedTodo = BaseTodo.extend({});\n"
            "const todo = new SpecializedTodo({ id: 7 });\n"
            "todo.fetch();\n"
        ),
    })

    [call] = [
        s for s in statements
        if s.semanticType == "api_call" and s.framework == "backbone"
    ]
    assert (call.method, call.endpoint) == ("GET", "/todos/:id")


def test_prototype_urlroot_is_used_for_model_calls(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const Todo = Backbone.Model.extend({});\n"
        "Todo.prototype.urlRoot = '/api/todos';\n"
        "const todo = new Todo({ id: 7 });\n"
        "todo.fetch();\n",
    )

    [call] = [
        s for s in statements
        if s.semanticType == "api_call" and s.framework == "backbone"
    ]
    assert (call.method, call.endpoint) == ("GET", "/api/todos/:id")


def test_inline_model_instance_fetch_is_captured(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const Todo = Backbone.Model.extend({ urlRoot: '/todos' });\n"
        "new Todo({ id: 7 }).fetch();\n",
    )

    [call] = [
        s for s in statements
        if s.semanticType == "api_call" and s.framework == "backbone"
    ]
    assert (call.method, call.endpoint) == ("GET", "/todos/:id")


def test_custom_model_methods_are_not_captured_as_backbone_calls(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const Todo = Backbone.Model.extend({\n"
        "  urlRoot: '/todos',\n"
        "  save() {},\n"
        "});\n"
        "const todo = new Todo({ id: 7 });\n"
        "todo.save();\n",
    )

    assert not any(
        s.semanticType == "api_call"
        and s.framework == "backbone"
        and "todo.save" in (s.text or "")
        for s in statements
    )


def test_save_verb_is_unknown_once_the_model_may_have_gained_an_id(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const Todo = Backbone.Model.extend({ urlRoot: '/todos' });\n"
        "const a = new Todo();\n"
        "a.set('id', 1);\n"
        "a.save();\n"
        "const b = new Todo();\n"
        "b.save();\n"
        "b.save();\n"
        "const c = new Todo();\n"
        "c.save();\n",
    )

    saves = {s.text.split(".")[0].strip(): s for s in statements
             if s.semanticType == "api_call" and s.framework == "backbone" and ".save" in (s.text or "")}
    assert not getattr(saves["a"], "method", None)
    assert not getattr(saves["a"], "endpoint", None)
    assert getattr(saves["c"], "method", None) == "POST"


def test_marionette_mn_alias_routes_and_views_are_captured(analyze_files) -> None:
    statements = _backbone(
        analyze_files,
        "const R = Mn.AppRouter.extend({ appRoutes: { 'items/:id': 'show' } });\n"
        "const V = Mn.View.extend({ template: false });\n"
        "const Other = Mn.Object.extend({});\n",
    )

    assert [s.endpoint for s in statements if s.semanticType == "route"] == ["/items/:id"]
    assert sum(1 for s in statements if getattr(s, "uiRole", None) == "view") == 1


def test_mn_without_the_marionette_shape_is_not_backbone(analyze_files) -> None:
    statements = analyze_files({"src/app.js": "const x = Mn.Foo.extend({ routes: { a: 'b' } });\n"})

    assert not [s for s in statements if s.semanticType == "route"]
