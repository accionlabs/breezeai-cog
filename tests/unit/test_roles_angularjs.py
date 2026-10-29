"""AngularJS role table: docs/entry-point-roles/angularjs.md — one test per row."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

ROUTES = "app/routes.js"


def _routes(analyze_files, body: str, **extra: str):
    src = (
        "angular.module('shop', ['ngRoute']).config(['$routeProvider', function ($routeProvider) {\n"
        f"{body}"
        "}]);\n"
    )
    return analyze_files({ROUTES: src, **extra})


def _records(stmts, path: str = ROUTES):
    return [s for s in stmts if s.path == path and s.semanticType == "route"]


def _eps(stmts, path: str = ROUTES):
    return [s for s in _records(stmts, path) if is_entry_point(s)]


# --- ngRoute -----------------------------------------------------------------------------------


def test_when_with_a_controller_is_a_binding(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  $routeProvider.when('/products/:id', { templateUrl: 'd.html', controller: 'DetailCtrl' });\n",
    )
    [r] = _eps(st)
    assert (r.semanticType, r.routeKind, r.framework, r.endpoint, r.handler, r.nodeType) == (
        "route",
        "page",
        "angularjs",
        "/products/:id",
        "DetailCtrl",
        "call_expression",
    )
    assert (r.startLine, r.endLine) == (2, 2)
    assert r.text.startswith("when('/products/:id', {")


def test_controller_alias_is_dropped(analyze_files) -> None:
    st = _routes(analyze_files, "  $routeProvider.when('/a', { controller: 'ListCtrl as vm' });\n")
    [r] = _eps(st)
    assert r.handler == "ListCtrl"


def test_inline_controller_is_anonymous(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  $routeProvider.when('/a', {\n    controller: function ($scope) {}\n  });\n"
        "  $routeProvider.when('/b', { controller: ['$scope', function ($scope) {}] });\n",
    )
    a, b = sorted(_eps(st), key=lambda s: s.endpoint or "")
    assert (a.endpoint, a.handler, a.handlerLine) == ("/a", None, 3)
    assert (b.endpoint, b.handler, b.handlerLine) == ("/b", None, 5)


def test_controller_function_in_the_file_is_located(analyze_files) -> None:
    src = (
        "angular.module('shop').config(function ($routeProvider) {\n"
        "  $routeProvider.when('/a', { controller: AboutCtrl });\n"
        "});\n"
        "function AboutCtrl($scope) {}\n"
    )
    [r] = _eps(analyze_files({ROUTES: src}))
    assert (r.handler, r.handlerLine, r.handlerPath) == ("AboutCtrl", 4, None)


def test_template_only_route_has_no_handler(analyze_files) -> None:
    st = _routes(analyze_files, "  $routeProvider.when('/help', { templateUrl: 'help.html' });\n")
    [r] = _eps(st)
    assert (r.routeKind, r.endpoint, r.handler) == ("page", "/help", None)


def test_redirect_is_navigation_not_an_entry_point(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  $routeProvider.when('/old', { redirectTo: '/products?tab=1' });\n"
        "  $routeProvider.when('/fn', { redirectTo: function () { return '/x'; } });\n"
        "  $routeProvider.otherwise({ redirectTo: '/products' });\n"
        "  $routeProvider.otherwise('/home#top');\n",
    )
    assert not _eps(st)
    got = sorted((r.startLine, r.routeKind, r.endpoint, r.handler) for r in _records(st))
    assert got == [
        (2, "navigation", "/products", None),
        (3, "navigation", None, None),
        (4, "navigation", "/products", None),
        (5, "navigation", "/home", None),
    ]


def test_catch_all_view_is_not_recorded(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  $routeProvider.otherwise({ templateUrl: '404.html', controller: 'NotFoundCtrl' });\n",
    )
    assert not _records(st)


def test_chain_gives_one_record_per_call(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  $routeProvider\n"
        "    .when('/a', { controller: 'A' })\n"
        "    .when('/b', { controller: 'B' })\n"
        "    .otherwise({ redirectTo: '/a' });\n",
    )
    got = [(r.startLine, r.routeKind, r.endpoint, r.handler) for r in _records(st)]
    assert sorted(got) == [
        (3, "page", "/a", "A"),
        (4, "page", "/b", "B"),
        (5, "navigation", "/a", None),
    ]
    assert len({r.id for r in _records(st)}) == 3


def test_path_that_is_not_a_literal_is_left_empty(analyze_files) -> None:
    st = _routes(analyze_files, "  $routeProvider.when('/a/' + ID, { controller: 'A' });\n")
    [r] = _eps(st)
    assert (r.endpoint, r.handler) == (None, "A")


def test_optional_parameter_marker_is_kept(analyze_files) -> None:
    st = _routes(analyze_files, "  $routeProvider.when('/items/:id?', { controller: 'I' });\n")
    [r] = _eps(st)
    assert r.endpoint == "/items/:id?"


def test_when_with_a_variable_config_is_not_recorded(analyze_files) -> None:
    st = _routes(analyze_files, "  $routeProvider.when('/a', config);\n")
    assert not _records(st)


def test_component_route_names_the_component(analyze_files) -> None:
    st = _routes(analyze_files, "  $routeProvider.when('/a', { component: 'userList' });\n")
    [r] = _eps(st)
    assert r.handler == "userList"


def test_fixture_files_are_skipped(analyze_files) -> None:
    # `.e2e.` files are captured (structure) but never emit routes; `.spec.` files are not scanned.
    src = "$routeProvider.when('/a', { controller: 'A' });\n"
    st = analyze_files({"app/routes.e2e.js": src, "app/routes.js": src})
    assert len(_records(st, "app/routes.js")) == 1
    assert not _records(st, "app/routes.e2e.js")


# --- registrations -----------------------------------------------------------------------------

CTRL = "app/controllers.js"


def _registered(stmts, path: str = CTRL):
    return sorted(
        (s.startLine, s.name, s.uiRole, s.framework)
        for s in stmts
        if s.path == path and s.uiRole is not None
    )


def test_registrations_on_a_module_chain_are_tagged(analyze_files) -> None:
    src = (
        "angular.module('shop')\n"
        "  .controller('ListCtrl', function ($scope) {})\n"
        "  .service('CartService', function () {})\n"
        "  .factory('Product', function () {})\n"
        "  .directive('priceTag', function () { return {}; })\n"
        "  .component('cartBadge', { templateUrl: 'badge.html' });\n"
    )
    assert _registered(analyze_files({CTRL: src})) == [
        (2, "ListCtrl", "controller", "angularjs"),
        (5, "priceTag", "directive", "angularjs"),
        (6, "cartBadge", "component", "angularjs"),
    ]


def test_registration_on_a_module_variable_is_tagged(analyze_files) -> None:
    src = (
        "var app = angular.module('shop', []);\n"
        "app.controller('A', function () {});\n"
        "var other;\n"
        "other = angular.module('x').config(function () {});\n"
        "other.directive('b', function () {});\n"
    )
    assert _registered(analyze_files({CTRL: src})) == [
        (2, "A", "controller", "angularjs"),
        (5, "b", "directive", "angularjs"),
    ]


def test_registration_on_an_unproved_receiver_is_not_tagged(analyze_files) -> None:
    src = (
        "angular.module('shop');\n"
        "app.controller('A', function () {});\n"
        "router.component('b', {});\n"
        "angular.module('shop').controller(NAME, function () {});\n"
    )
    assert _registered(analyze_files({CTRL: src})) == []


# --- a route's controller in another file ------------------------------------------------------


def _located(stmts):
    return {(r.endpoint): (r.handler, r.handlerPath, r.handlerLine) for r in _eps(stmts)}


def test_controller_registered_in_another_file_is_located(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  $routeProvider.when('/list', { controller: 'ListCtrl as vm' });\n"
        "  $routeProvider.when('/detail', { controller: 'DetailCtrl' });\n",
        **{
            CTRL: (
                "angular.module('shop')\n"
                "  .controller('ListCtrl', function ($scope) {\n"
                "  })\n"
                "  .controller('DetailCtrl', DetailCtrl);\n"
                "function DetailCtrl($scope) {}\n"
            )
        },
    )
    assert _located(st) == {
        "/list": ("ListCtrl", CTRL, 2),
        "/detail": ("DetailCtrl", CTRL, 5),
    }


def test_component_registered_in_another_file_is_located(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  $routeProvider.when('/c', { component: 'cartBadge' });\n",
        **{CTRL: "angular.module('shop')\n  .component('cartBadge', {\n    template: 'x'\n  });\n"},
    )
    assert _located(st) == {"/c": ("cartBadge", CTRL, 2)}


def test_controller_in_the_route_file_has_no_handler_path(analyze_files) -> None:
    src = (
        "angular.module('shop').config(function ($routeProvider) {\n"
        "  $routeProvider.when('/a', { controller: 'AboutCtrl' });\n"
        "}).controller('AboutCtrl', function () {});\n"
    )
    assert _located(analyze_files({ROUTES: src})) == {"/a": ("AboutCtrl", None, 3)}


def test_controller_registered_twice_is_not_located(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  $routeProvider.when('/a', { controller: 'Dup' });\n",
        **{
            CTRL: "angular.module('a').controller('Dup', function () {});\n",
            "app/other.js": "angular.module('b').controller('Dup', function () {});\n",
        },
    )
    assert _located(st) == {"/a": ("Dup", None, None)}


def test_controller_with_an_unreadable_registration_is_not_located(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  $routeProvider.when('/a', { controller: 'Imp' });\n",
        **{CTRL: "import { Imp } from './imp';\nangular.module('a').controller('Imp', Imp);\n"},
    )
    assert _located(st) == {"/a": ("Imp", None, None)}


def test_same_named_function_in_the_route_file_is_not_a_guess(analyze_files) -> None:
    # `controller: 'Foo'` is a registered name: an unrelated `function Foo` in the file is not it.
    src = (
        "angular.module('shop').config(function ($routeProvider) {\n"
        "  $routeProvider.when('/a', { controller: 'Foo' });\n"
        "});\n"
        "function Foo() {}\n"
    )
    assert _located(analyze_files({ROUTES: src})) == {"/a": ("Foo", None, None)}


# --- ui-router ---------------------------------------------------------------------------------

STATES = "app/states.js"


def _states(analyze_files, body: str, **extra: str):
    src = f"angular.module('shop').config(function ($stateProvider, $urlRouterProvider) {{\n{body}}});\n"
    return analyze_files({STATES: src, **extra})


def _pages(stmts, path: str = STATES):
    return {(r.endpoint, r.handler): r for r in _eps(stmts, path)}


def test_state_with_a_url_is_a_binding(analyze_files) -> None:
    st = _states(
        analyze_files,
        "  $stateProvider.state('users', { url: '/users/:id', controller: 'UserCtrl' });\n",
    )
    [r] = _eps(st, STATES)
    assert (r.semanticType, r.routeKind, r.framework, r.endpoint, r.handler) == (
        "route",
        "page",
        "angularjs",
        "/users/:id",
        "UserCtrl",
    )


def test_child_state_joins_the_parent_url(analyze_files) -> None:
    st = _states(
        analyze_files,
        "  $stateProvider\n"
        "    .state('app', { abstract: true, url: '/app', templateUrl: 'layout.html' })\n"
        "    .state('app.users', { url: '/users' })\n"
        "    .state('app.users.detail', { url: '/:id' })\n"
        "    .state('admin', { parent: 'app', url: '/admin' })\n"
        "    .state('home', { url: '^/home', parent: 'app' });\n",
    )
    assert sorted(r.endpoint for r in _eps(st, STATES)) == [
        "/app/admin",
        "/app/users",
        "/app/users/:id",
        "/home",
    ]


def test_abstract_state_and_state_without_a_url_have_no_record(analyze_files) -> None:
    st = _states(
        analyze_files,
        "  $stateProvider.state('a', { abstract: true, url: '/a' });\n"
        "  $stateProvider.state('b', { templateUrl: 'b.html' });\n"
        "  $stateProvider.state('c', { url: '/c', redirectTo: 'a' });\n",
    )
    assert not _records(st, STATES)


def test_parent_in_another_file_gives_the_address(analyze_files) -> None:
    st = _states(
        analyze_files,
        "  $stateProvider.state('app.users', { url: '/users' });\n",
        **{
            "app/parent.js": "angular.module('shop').config(function ($stateProvider) {\n"
            "  $stateProvider.state('app', { abstract: true, url: '/app' });\n});\n"
        },
    )
    [r] = _eps(st, STATES)
    assert r.endpoint == "/app/users"


def test_unresolved_parent_leaves_the_address_empty(analyze_files) -> None:
    # not declared anywhere / declared twice differently / a url that isn't a literal
    st = _states(
        analyze_files,
        "  $stateProvider.state('ghost.child', { url: '/c' });\n"
        "  $stateProvider.state('dup.child', { url: '/d' });\n"
        "  $stateProvider.state('dyn.child', { url: '/e' });\n"
        "  $stateProvider.state('dyn', { url: PREFIX + '/dyn' });\n",
        **{
            "app/dup1.js": "$stateProvider.state('dup', { url: '/one' });\n",
            "app/dup2.js": "$stateProvider.state('dup', { url: '/two' });\n",
        },
    )
    assert {r.handler: r.endpoint for r in _eps(st, STATES)} == {None: None}
    assert all(r.endpoint is None for r in _eps(st, STATES))
    assert len(_eps(st, STATES)) == 4


def test_query_declaration_is_cut_and_params_are_kept(analyze_files) -> None:
    st = _states(
        analyze_files,
        "  $stateProvider.state('list', { url: '/items?page&sort' });\n"
        "  $stateProvider.state('item', { url: '/items/{id:int}' });\n",
    )
    assert sorted(r.endpoint for r in _eps(st, STATES)) == ["/items", "/items/{id:int}"]


def test_object_form_state(analyze_files) -> None:
    st = _states(
        analyze_files, "  $stateProvider.state({ name: 'x', url: '/x', component: 'xPage' });\n"
    )
    [r] = _eps(st, STATES)
    assert (r.endpoint, r.handler) == ("/x", "xPage")


def test_state_controller_registered_in_another_file_is_located(analyze_files) -> None:
    st = _states(
        analyze_files,
        "  $stateProvider.state('u', { url: '/u', controller: 'UCtrl as vm' });\n",
        **{CTRL: "\nangular.module('shop').controller('UCtrl', function () {});\n"},
    )
    [r] = _eps(st, STATES)
    assert (r.handler, r.handlerPath, r.handlerLine) == ("UCtrl", CTRL, 2)


def test_views_name_a_handler_only_when_exactly_one_does(analyze_files) -> None:
    st = _states(
        analyze_files,
        "  $stateProvider.state('one', { url: '/one', views: {\n"
        "    'main@': { controller: 'MainCtrl' }, 'side@': { templateUrl: 's.html' } } });\n"
        "  $stateProvider.state('two', { url: '/two', views: {\n"
        "    'a@': { controller: 'A' }, 'b@': { controller: 'B' } } });\n"
        "  $stateProvider.state('three', { url: '/three', views: viewsFor() });\n",
    )
    assert {r.endpoint: r.handler for r in _eps(st, STATES)} == {
        "/one": "MainCtrl",
        "/two": None,
        "/three": None,
    }


def test_url_router_otherwise_is_navigation(analyze_files) -> None:
    st = _states(
        analyze_files,
        "  $urlRouterProvider.otherwise('/home');\n"
        "  $urlRouterProvider.otherwise(function ($injector) { return '/x'; });\n",
    )
    assert not _eps(st, STATES)
    assert sorted((r.startLine, r.routeKind, r.endpoint) for r in _records(st, STATES)) == [
        (2, "navigation", "/home"),
        (3, "navigation", None),
    ]


def test_state_config_that_is_not_an_object_is_not_recorded(analyze_files) -> None:
    st = _states(
        analyze_files,
        "  $stateProvider.state('a', config);\n  $stateProvider.state(NAME, { url: '/n' });\n",
    )
    assert not _records(st, STATES)


# --- $resource ---------------------------------------------------------------------------------

SERVICES = "app/services.js"


def _api(stmts, path: str = SERVICES):
    return sorted(
        (s.startLine, s.endpoint, s.method, s.nodeType)
        for s in stmts
        if s.path == path and s.semanticType == "api_call" and s.framework == "angularjs"
    )


def test_resource_is_an_api_call_without_a_method(analyze_files) -> None:
    src = (
        "var Local = $resource('/api/local/:id');\n"
        "angular.module('shop')\n"
        "  .factory('Product', function ($resource) {\n"
        "    return $resource('/api/products/:id', { id: '@id' }, { update: { method: 'PUT' } });\n"
        "  });\n"
    )
    assert _api(analyze_files({SERVICES: src})) == [
        (1, "/api/local/:id", None, "call_expression"),
        (4, "/api/products/:id", None, "call_expression"),
    ]


def test_resource_url_that_is_not_a_literal_is_left_empty(analyze_files) -> None:
    src = "var A = $resource(BASE + '/a');\nvar B = $resource();\nthis.$resource('/c');\nresource('/d');\n"
    assert _api(analyze_files({SERVICES: src})) == [
        (1, None, None, "call_expression"),
        (2, None, None, "call_expression"),
    ]


# --- a controller or component imported from another module -----------------------------------


def test_controller_registered_from_an_import_is_followed(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  $routeProvider.when('/a', { controller: 'ACtrl' });\n"
        "  $routeProvider.when('/b', { controller: 'BCtrl' });\n"
        "  $routeProvider.when('/c', { controller: 'CCtrl' });\n"
        "  $routeProvider.when('/h', { component: 'appHeader' });\n",
        **{
            CTRL: (
                "import angular from 'angular';\n"
                "import ACtrl from './a.controller';\n"
                "import { BCtrl as Renamed } from './b.controller';\n"
                "import CCtrl from './c.controller';\n"
                "import AppHeader from './header.component';\n"
                "const m = angular.module('shop', []);\n"
                "m.controller('ACtrl', ACtrl);\n"
                "m.controller('BCtrl', Renamed);\n"
                "m.controller('CCtrl', ['$scope', CCtrl]);\n"
                "m.component('appHeader', AppHeader);\n"
            ),
            "app/a.controller.js": "\nclass ACtrl {}\nexport default ACtrl;\n",
            "app/b.controller.js": "export function BCtrl() {}\n",
            "app/c.controller.js": "export default function CCtrl($scope) {}\n",
            "app/header.component.js": "\n\nexport default { template: '<h1/>' };\n",
        },
    )
    got = _located(st)
    assert got["/a"] == ("ACtrl", "app/a.controller.js", 2)
    assert got["/b"] == ("BCtrl", "app/b.controller.js", 1)
    assert got["/c"] == ("CCtrl", "app/c.controller.js", 1)
    assert got["/h"] == ("appHeader", "app/header.component.js", 3)


# --- found in review: layouts and shapes the first version missed -----------------------------


def test_registration_written_across_lines_is_found(analyze_files) -> None:
    # `angular` and `.module(…)` on separate lines: the usual style-guide layout.
    st = _routes(
        analyze_files,
        "  $routeProvider.when('/a', { controller: 'Avengers' });\n",
        **{
            CTRL: (
                "(function () {\n  angular\n    .module('app')\n    .controller('Avengers', Avengers);\n"
                "  function Avengers() {}\n})();\n"
            )
        },
    )
    assert _located(st) == {"/a": ("Avengers", CTRL, 5)}
    assert _registered(st) == [(4, "Avengers", "controller", "angularjs")]


def test_chained_registrations_on_a_module_variable_are_all_found(analyze_files) -> None:
    src = (
        "var app = angular.module('a', []);\n"
        "app.controller('A', function () {})\n"
        "  .controller('B', function () {})\n"
        "  .directive('cDir', function () {})\n"
        "  .component('dComp', { template: 'x' });\n"
    )
    assert [r[1:3] for r in _registered(analyze_files({CTRL: src}))] == [
        ("A", "controller"),
        ("B", "controller"),
        ("cDir", "directive"),
        ("dComp", "component"),
    ]


def test_resource_in_a_one_line_chain_does_not_relabel_the_chain(analyze_files) -> None:
    src = "angular.module('a').factory('Res', function ($resource) { return $resource('/api/one'); });\n"
    st = [s for s in analyze_files({SERVICES: src}) if s.path == SERVICES]
    assert [s.nodeType for s in st if s.semanticType == "api_call"] == ["call_expression"]
    assert [s.semanticType for s in st if s.nodeType == "expression_statement"] == [None]


def test_class_and_object_registered_in_the_same_file_are_located(analyze_files) -> None:
    ts = "app/controllers.ts"
    src = (
        "class ListCtrl {}\n"
        "const badge = {\n  template: 'x',\n};\n"
        "angular.module('shop').controller('ListCtrl', ListCtrl).component('badge', badge);\n"
    )
    st = _routes(
        analyze_files,
        "  $routeProvider.when('/l', { controller: 'ListCtrl' });\n"
        "  $routeProvider.when('/b', { component: 'badge' });\n",
        **{ts: src},
    )
    assert _located(st) == {"/l": ("ListCtrl", ts, 1), "/b": ("badge", ts, 2)}


def test_renamed_import_is_named_as_the_typescript_linker_names_it(analyze_files) -> None:
    src = (
        "import { RenamedCtrl as Local } from './r';\n"
        "angular.module('shop').config(function ($routeProvider) {\n"
        "  $routeProvider.when('/a', { controller: Local });\n"
        "});\n"
    )
    st = analyze_files({ROUTES: src, "app/r.js": "export function RenamedCtrl() {}\n"})
    assert _located(st) == {"/a": ("RenamedCtrl", "app/r.js", 1)}


def test_resource_defined_in_the_file_is_not_the_angular_service(analyze_files) -> None:
    src = "const $resource = (u) => fetch(u);\n$resource('/not/angular');\n"
    st = analyze_files({SERVICES: src})
    assert not [s for s in st if s.framework == "angularjs"]


def test_address_without_a_leading_slash_is_left_empty(analyze_files) -> None:
    st = _routes(analyze_files, "  $routeProvider.when('rel', { controller: 'A' });\n")
    [r] = _eps(st)
    assert r.endpoint is None
    st = _states(analyze_files, "  $stateProvider.state('s', { url: 'rel' });\n")
    [r] = _eps(st, STATES)
    assert r.endpoint is None


def test_state_cycle_abstract_flag_and_unreadable_parent_views(analyze_files) -> None:
    st = _states(
        analyze_files,
        "  $stateProvider.state('a', { parent: 'b', url: '/a' });\n"
        "  $stateProvider.state('b', { parent: 'a', url: '/b' });\n"
        "  $stateProvider.state('c', { abstract: IS_ABSTRACT, url: '/c' });\n"
        "  $stateProvider.state('d', { parent: PARENT, url: '/d' });\n"
        "  $stateProvider.state('e', { url: '/e', views: { main: viewConfig } });\n"
        "  $stateProvider.state('f', { url: '/f', views: VIEWS });\n",
    )
    assert {r.endpoint: r.handler for r in _eps(st, STATES)} == {None: None, "/e": None, "/f": None}
    # a, b (a cycle) and d (a parent that is not a literal) are pages with no address; c may be abstract
    assert len(_eps(st, STATES)) == 5


def test_no_records_without_statement_capture(tmp_path) -> None:
    from breezeai_cog import iter_file_records

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.js").write_text(
        "angular.module('a').controller('C', function () {})"
        ".config(function ($routeProvider) { $routeProvider.when('/x', { controller: 'C' }); });\n"
        "var R = $resource('/r');\n"
    )
    records = list(iter_file_records(repo, capture_statements=False))
    assert not [s for r in records for s in r.statements]
