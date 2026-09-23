"""PHP framework tests: Laravel, Slim, Symfony, WordPress, Eloquent, Doctrine, PDO, WPDB, Guzzle."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import function_id, to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.php.parser import PhpParser
from breezeai_cog.parsers.php_codeigniter.parser import CodeIgniterParser
from breezeai_cog.parsers.php_laravel.parser import LaravelParser
from breezeai_cog.parsers.php_slim.parser import SlimParser
from breezeai_cog.parsers.php_symfony.parser import SymfonyParser
from breezeai_cog.schemas import FileRecord


def _parse(parser_cls, tmp_path: Path, src: bytes, rel: str) -> FileRecord:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(src)
    parser = parser_cls()
    ctx = ParseContext(
        path=rel,
        abs_path=p,
        source=src,
        repo_root=tmp_path,
        capture_statements=True,
    )
    return parser.parse_file(ctx)


def _route_shape(route) -> tuple[str | None, str | None, str | None, str | None, str]:
    return route.method, route.endpoint, route.handler, route.routeKind, route.parentId


def test_laravel_routes(tmp_path: Path) -> None:
    src = b"""<?php
namespace App\\Routes;

use Illuminate\\Support\\Facades\\Route;

Route::get('/users', 'UserController@index');
Route::post('/users', 'UserController@store');
Route::match(['GET', 'POST'], '/profile', 'ProfileController@handle');
Route::resource('photos', 'PhotoController');
"""
    parser = LaravelParser()
    assert parser.claims("routes/web.php", src) is True

    rec = _parse(LaravelParser, tmp_path, src, "routes/web.php")
    assert rec.framework == "laravel"

    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert [_route_shape(route) for route in routes] == [
        ("GET", "/users", "UserController@index", "route", rec.id),
        ("POST", "/users", "UserController@store", "route", rec.id),
        ("GET", "/profile", "ProfileController@handle", "route", rec.id),
        ("ANY", "photos", None, "route", rec.id),
        ("POST", "/profile", "ProfileController@handle", "route", rec.id),
    ]


def test_laravel_middleware_guards(tmp_path: Path) -> None:
    src = b"""<?php
use Illuminate\\Support\\Facades\\Route;
Route::get('/admin', 'AdminController@index')->middleware(['auth', 'verified']);
"""
    rec = _parse(LaravelParser, tmp_path, src, "routes/web.php")
    route = next(s for s in rec.statements if s.semanticType == "route")
    assert route.guards == ["auth", "verified"]


def test_slim_routes(tmp_path: Path) -> None:
    src = b"""<?php
use Slim\\Factory\\AppFactory;

$app = AppFactory::create();
$app->get('/api/users', function ($req, $res) { return $res; });
$app->post('/api/users', function ($req, $res) { return $res; });
$app->map(['GET', 'POST'], '/api/items', function ($req, $res) { return $res; });
"""
    parser = SlimParser()
    assert parser.claims("src/index.php", src) is True

    rec = _parse(SlimParser, tmp_path, src, "src/index.php")
    assert rec.framework == "slim"

    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert [_route_shape(route) for route in routes] == [
        ("GET", "/api/users", None, "route", rec.id),
        ("POST", "/api/users", None, "route", rec.id),
        ("GET", "/api/items", None, "route", rec.id),
        ("POST", "/api/items", None, "route", rec.id),
    ]


def test_slim_container_get_is_not_a_route(tmp_path: Path) -> None:
    src = b"""<?php
use Slim\Factory\AppFactory;

$app = AppFactory::create();
$service = $app->get(SomeService::class);
"""
    rec = _parse(SlimParser, tmp_path, src, "src/index.php")

    assert [s for s in rec.statements if s.semanticType == "route"] == []


def test_slim_route_on_this_app_property(tmp_path: Path) -> None:
    src = b"""<?php
use Slim\Factory\AppFactory;

class Routes
{
    private $app;

    public function register()
    {
        $this->app->get('/x', function ($req, $res) { return $res; });
    }
}
"""
    rec = _parse(SlimParser, tmp_path, src, "src/index.php")
    routes = [s for s in rec.statements if s.semanticType == "route"]

    assert len(routes) == 1
    assert routes[0].endpoint == "/x"
    assert routes[0].method == "GET"


def test_symfony_routes(tmp_path: Path) -> None:
    src = b"""<?php
namespace App\\Controller;

use Symfony\\Component\\Routing\\Annotation\\Route;

#[Route('/api')]
class UserController
{
    #[Route('/users', methods: ['GET', 'HEAD'])]
    public function list() {}

    #[Route('/users/{id}', methods: ['POST'])]
    public function update(int $id) {}
}
"""
    parser = SymfonyParser()
    assert parser.claims("src/Controller/UserController.php", src) is True

    rec = _parse(SymfonyParser, tmp_path, src, "src/Controller/UserController.php")
    assert rec.framework == "symfony"

    routes = [s for s in rec.statements if s.semanticType == "route"]
    list_fn = next(fn for fn in rec.functions if fn.name == "list")
    update_fn = next(fn for fn in rec.functions if fn.name == "update")
    assert [_route_shape(route) for route in routes] == [
        ("GET", "/api/users", "UserController@list", "route", list_fn.id),
        ("HEAD", "/api/users", "UserController@list", "route", list_fn.id),
        ("POST", "/api/users/{id}", "UserController@update", "route", update_fn.id),
    ]


def test_symfony_docblock_route_annotation(tmp_path: Path) -> None:
    src = b"""<?php
namespace App\\Controller;

use Symfony\\Component\\Routing\\Annotation\\Route;

class LegacyController
{
    /** @Route("/legacy", methods={"GET", "POST"}) */
    public function index() {}
}
"""
    rec = _parse(SymfonyParser, tmp_path, src, "src/Controller/LegacyController.php")
    routes = [s for s in rec.statements if s.semanticType == "route"]

    assert {(route.method, route.endpoint) for route in routes} == {
        ("GET", "/legacy"),
        ("POST", "/legacy"),
    }


def test_symfony_route_path_colon_is_preserved(tmp_path: Path) -> None:
    src = b"""<?php
use Symfony\\Component\\Routing\\Attribute\\Route;

class LegacyController
{
    #[Route('legacy:format', methods: ['GET'])]
    public function index() {}
}
"""
    rec = _parse(SymfonyParser, tmp_path, src, "src/Controller/LegacyController.php")
    route = next(s for s in rec.statements if s.semanticType == "route")

    assert route.endpoint == "/legacy:format"


def test_symfony_route_priority_without_path_is_not_path(tmp_path: Path) -> None:
    src = b"""<?php
use Symfony\\Component\\Routing\\Attribute\\Route;

class LegacyController
{
    #[Route(priority: 10, methods: ['GET'])]
    public function index() {}
}
"""
    rec = _parse(SymfonyParser, tmp_path, src, "src/Controller/LegacyController.php")
    route = next(s for s in rec.statements if s.semanticType == "route")

    assert route.endpoint == "/"
    assert route.endpoint != "/10"


def test_symfony_is_granted_guard(tmp_path: Path) -> None:
    src = b"""<?php
namespace App\\Controller;
use Symfony\\Component\\Routing\\Attribute\\Route;
use Symfony\\Component\\Security\\Http\\Attribute\\IsGranted;
class AdminController
{
    #[Route('/admin')]
    #[IsGranted('ROLE_ADMIN')]
    public function index() {}
}
"""
    rec = _parse(SymfonyParser, tmp_path, src, "src/Controller/AdminController.php")
    route = next(s for s in rec.statements if s.semanticType == "route")
    assert route.guards == ["ROLE_ADMIN"]


def test_symfony_route_attribute_is_not_retained_as_decorator(tmp_path: Path) -> None:
    src = b"""<?php
namespace App\\Controller;

use Symfony\\Component\\Routing\\Attribute\\Route;

class ExampleController
{
    #[Route('/x')]
    #[Audit]
    public function show() {}
}
"""
    rec = _parse(SymfonyParser, tmp_path, src, "src/Controller/ExampleController.php")
    route = next(s for s in rec.statements if s.semanticType == "route")
    assert route.endpoint == "/x"
    fn = next(fn for fn in rec.functions if fn.name == "show")
    assert [decorator.name for decorator in fn.decorators] == ["Audit"]


def test_codeigniter4_routes(tmp_path: Path) -> None:
    src = b"""<?php
namespace Config;

use CodeIgniter\\Config\\BaseService;

$routes->get('/users', 'UserController::index');
$routes->post('/users', 'UserController::store');
$routes->match(['get', 'post'], '/profile', 'ProfileController::show');
$routes->resource('photos');
$routes->group('admin', function ($routes) {
    $routes->get('dashboard', 'AdminController::dashboard');
});
"""
    parser = CodeIgniterParser()
    assert parser.claims("app/Config/Routes.php", src) is True

    rec = _parse(CodeIgniterParser, tmp_path, src, "app/Config/Routes.php")
    assert rec.framework == "codeigniter"

    routes = [s for s in rec.statements if s.semanticType == "route"]
    group_fn = next(fn for fn in rec.functions if fn.type == "function_expression")
    assert [_route_shape(route) for route in routes] == [
        ("GET", "/admin/dashboard", None, "route", rec.id),
        ("GET", "/users", None, "route", rec.id),
        ("POST", "/users", None, "route", rec.id),
        ("GET", "/profile", None, "route", rec.id),
        ("ANY", "photos", None, "route", rec.id),
        ("POST", "/profile", None, "route", rec.id),
    ]


def test_codeigniter3_routes(tmp_path: Path) -> None:
    src = b"""<?php
defined('BASEPATH') OR exit('No direct script access allowed');

$route['default_controller'] = 'welcome';
$route['404_override'] = '';
$route['translate_uri_dashes'] = FALSE;

$route['journals'] = 'blogs';
$route['product/(:any)'] = 'catalog/product_lookup';
$route['products']['get'] = 'catalog/index';
$route['products']['post'] = 'catalog/create';
"""
    parser = CodeIgniterParser()
    assert parser.claims("application/config/routes.php", src) is True

    rec = _parse(CodeIgniterParser, tmp_path, src, "application/config/routes.php")
    assert rec.framework == "codeigniter"

    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert all(r.routeKind == "route" for r in routes)
    endpoints = {r.endpoint: r for r in routes}

    assert "default_controller" not in endpoints
    assert "404_override" not in endpoints
    assert "journals" in endpoints
    assert endpoints["journals"].method == "ANY"
    assert "product/(:any)" in endpoints
    assert "products" in endpoints
    assert any(r.endpoint == "products" and r.method == "GET" for r in routes)
    assert any(r.endpoint == "products" and r.method == "POST" for r in routes)



def test_wordpress_hooks(tmp_path: Path) -> None:
    src = b"""<?php
add_action('init', 'my_custom_init');
add_filter('the_content', function ($content) {
    return $content . '<p>Footer</p>';
});
"""
    rec = _parse(PhpParser, tmp_path, src, "wp-content/plugins/my-plugin.php")
    hooks = [s for s in rec.statements if s.semanticType == "route" and s.routeKind == "eventbus_consumer"]
    assert len(hooks) == 2
    tags = {h.endpoint for h in hooks}
    assert "init" in tags
    assert "the_content" in tags
    assert all(h.method == "CONSUMER" for h in hooks)


def test_eloquent_detection(tmp_path: Path) -> None:
    src = b"""<?php
namespace App\\Http\\Controllers;

use App\\Models\\User;

class UserController
{
    public function show(int $id)
    {
        $user = User::where('active', true)->first();
        $other = User::find($id);
        $user->save();
        $created = User::create(['name' => 'Alice']);
    }
}
"""
    rec = _parse(PhpParser, tmp_path, src, "app/Http/Controllers/UserController.php")
    db_calls = [s for s in rec.statements if s.semanticType == "db_method_call"]
    assert len(db_calls) >= 3
    for call in db_calls:
        assert call.dataAccessHint == "eloquent"


def test_doctrine_detection(tmp_path: Path) -> None:
    src = b"""<?php
namespace App\\Service;

class UserService
{
    public function findUser($entityManager, int $id)
    {
        return $entityManager->getRepository(User::class)->find($id);
    }
}
"""
    rec = _parse(PhpParser, tmp_path, src, "src/Service/UserService.php")
    db_calls = [s for s in rec.statements if s.semanticType == "db_method_call"]
    assert len(db_calls) >= 1
    assert any(c.dataAccessHint == "doctrine" for c in db_calls)


def test_doctrine_query_builder_detection(tmp_path: Path) -> None:
    src = b"""<?php
class UserService
{
    public function findUsers($repo)
    {
        return $repo->createQueryBuilder('u');
    }
}
"""
    rec = _parse(PhpParser, tmp_path, src, "src/Service/UserService.php")
    db_calls = [s for s in rec.statements if s.semanticType == "db_method_call"]

    query_builder = next(s for s in db_calls if s.method == "createQueryBuilder")
    assert query_builder.dataAccessHint == "doctrine"


def test_pdo_queries(tmp_path: Path) -> None:
    src = b"""<?php
function queryData($pdo, $sql)
{
    $pdo->query('SELECT * FROM users');
    $pdo->prepare('SELECT * FROM users WHERE id = 1');
    $pdo->exec($sql);
}
"""
    rec = _parse(PhpParser, tmp_path, src, "src/db.php")
    queries = [s for s in rec.statements if s.semanticType == "query_statement"]
    assert len(queries) >= 3


def test_wpdb_queries(tmp_path: Path) -> None:
    src = b"""<?php
function getWpData($wpdb, $sql)
{
    $results = $wpdb->get_results($sql);
}
"""
    rec = _parse(PhpParser, tmp_path, src, "wp-content/themes/theme/db.php")
    queries = [s for s in rec.statements if s.semanticType == "query_statement"]
    assert len(queries) >= 1


def test_guzzle_api_calls(tmp_path: Path) -> None:
    src = b"""<?php
function fetchExternal($client)
{
    $client->get('https://example.com');
    $client->post('https://api.example.com/v1/items');
}
"""
    rec = _parse(PhpParser, tmp_path, src, "src/client.php")
    api_calls = [s for s in rec.statements if s.semanticType == "api_call"]
    assert len(api_calls) == 2
    endpoints = {c.endpoint for c in api_calls}
    assert "https://example.com" in endpoints
    assert "https://api.example.com/v1/items" in endpoints


def test_all_framework_records_validate_schema(tmp_path: Path) -> None:
    src = b"""<?php
namespace App\\Http\\Controllers;

use Illuminate\\Support\\Facades\\Route;
use App\\Models\\User;

Route::get('/users', function ($client) {
    $user = User::find(1);
    $client->get('https://example.com');
});
"""
    rec = _parse(LaravelParser, tmp_path, src, "routes/web.php")
    line = to_line(rec)
    data = json.loads(line)
    validator = Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
    errors = list(validator.iter_errors(data))
    assert not errors, [e.message for e in errors]


def test_controller_claims_priority_laravel_over_codeigniter() -> None:
    registry.discover_builtin()
    src = b"""<?php
namespace App\\Http\\Controllers;

use Illuminate\\Http\\Request;
use Symfony\\Component\\HttpFoundation\\Response;

class UserController extends BaseController
{
    public function index(Request $request): Response
    {
        return new Response("ok");
    }
}
"""
    path = "app/Http/Controllers/UserController.php"
    laravel = LaravelParser()
    codeigniter = CodeIgniterParser()
    symfony = SymfonyParser()
    slim = SlimParser()

    assert laravel.claims(path, src) is True
    assert codeigniter.claims(path, src) is False
    assert symfony.claims(path, src) is False
    assert slim.claims(path, src) is False

    # Selection via registry must select php-laravel, never php-codeigniter
    selected = registry.select(path, src)
    assert selected is not None
    assert selected.name == "php-laravel"
    assert selected.name != "php-codeigniter"


def test_framework_claims_via_composer_json(tmp_path: Path) -> None:
    registry.discover_builtin()
    # A controller without explicit Illuminate\\ import inside a Laravel repo
    composer = tmp_path / "composer.json"
    composer.write_text(json.dumps({"require": {"laravel/framework": "^10.0"}}))

    controller = tmp_path / "app/Http/Controllers/HomeController.php"
    controller.parent.mkdir(parents=True, exist_ok=True)
    src = b"""<?php
namespace App\\Http\\Controllers;

class HomeController
{
    public function index() { return "home"; }
}
"""
    controller.write_bytes(src)

    laravel = LaravelParser()
    codeigniter = CodeIgniterParser()
    assert laravel.claims(str(controller), src) is True
    assert codeigniter.claims(str(controller), src) is False

    selected = registry.select(str(controller), src)
    assert selected is not None
    assert selected.name == "php-laravel"


def test_route_match_multi_verb_capture(tmp_path: Path) -> None:
    src = b"""<?php
namespace App\\Routes;

use Illuminate\\Support\\Facades\\Route;

Route::match(['GET', 'POST'], '/profile', 'ProfileController@handle');
"""
    rec = _parse(LaravelParser, tmp_path, src, "routes/web.php")
    routes = [s for s in rec.statements if s.semanticType == "route" and s.endpoint == "/profile"]
    assert len(routes) == 2
    assert {r.method for r in routes} == {"GET", "POST"}

    # Plain single-string case in match
    src_single = b"""<?php
namespace App\\Routes;

use Illuminate\\Support\\Facades\\Route;

Route::match('GET', '/single', 'ProfileController@single');
"""
    rec_single = _parse(LaravelParser, tmp_path, src_single, "routes/single.php")
    routes_single = [s for s in rec_single.statements if s.semanticType == "route" and s.endpoint == "/single"]
    assert len(routes_single) == 1
    assert routes_single[0].method == "GET"


def test_laravel_prefix_group_resolves_route_endpoint(tmp_path: Path) -> None:
    src = b"""<?php
use Illuminate\\Support\\Facades\\Route;

Route::prefix('admin')->group(function () {
    Route::get('dashboard', 'AdminController@dashboard');
});
"""
    rec = _parse(LaravelParser, tmp_path, src, "routes/web.php")
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 1
    assert routes[0].endpoint == "/admin/dashboard"


def test_slim_nested_groups_resolve_route_endpoint(tmp_path: Path) -> None:
    src = b"""<?php
use Slim\\Factory\\AppFactory;

$app = AppFactory::create();
$app->group('/api', function ($group) {
    $group->group('/v1', function ($group) {
        $group->get('/users', function ($req, $res) { return $res; });
    });
});
"""
    rec = _parse(SlimParser, tmp_path, src, "src/index.php")
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 1
    assert routes[0].endpoint == "/api/v1/users"


def test_codeigniter4_group_with_options(tmp_path: Path) -> None:
    src = b"""<?php
namespace Config;

$routes->group('admin', ['filter' => 'auth'], function ($routes) {
    $routes->get('users', 'Admin\\Users::index');
});
"""
    rec = _parse(CodeIgniterParser, tmp_path, src, "app/Config/Routes.php")
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 1
    assert routes[0].endpoint == "/admin/users"
    assert routes[0].method == "GET"
    assert routes[0].handler is None
    assert routes[0].guards == ["auth"]


def test_codeigniter4_route_filter_option(tmp_path: Path) -> None:
    src = b"""<?php
namespace Config;

$routes->get('dashboard', 'Admin\\Dashboard::index', ['filter' => 'auth']);
"""
    rec = _parse(CodeIgniterParser, tmp_path, src, "app/Config/Routes.php")
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 1
    assert routes[0].endpoint == "dashboard"
    assert routes[0].guards == ["auth"]


def test_php_route_handler_resolution(tmp_path: Path) -> None:
    src = b"""<?php
namespace App\\Routes;

use App\\Http\\Controllers\\UserController;
use Illuminate\\Support\\Facades\\Route;

Route::get('/array-handler', [UserController::class, 'index']);
Route::get('/string-handler', 'WebhookController@handle');
Route::get('/closure-handler', function ($c) { return $c; });
Route::get('/var-handler', $dynamicHook);
"""
    rec = _parse(LaravelParser, tmp_path, src, "routes/web.php")
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}

    # [UserController::class, 'index'] -> "UserController@index"
    assert routes["/array-handler"].handler == "UserController@index"

    # 'WebhookController@handle' -> unchanged ("WebhookController@handle")
    assert routes["/string-handler"].handler == "WebhookController@handle"

    # a closure argument -> null
    assert routes["/closure-handler"].handler is None

    # bare variable argument ($dynamicHook) -> null
    assert routes["/var-handler"].handler is None


def test_codeigniter_render_url_non_literal_returns_none(tmp_path: Path) -> None:
    src = b"""<?php
namespace Config;

$routes->get($dynamicPath, 'WebhookController@handle');
"""
    rec = _parse(CodeIgniterParser, tmp_path, src, "app/Config/Routes.php")
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 1
    assert routes[0].endpoint is None
    assert routes[0].handler == "WebhookController@handle"


def test_wordpress_dynamic_hook_name_has_no_endpoint(tmp_path: Path) -> None:
    src = b"<?php\nadd_action($dynamicHook, 'register_dynamic_hook');\n"
    rec = _parse(PhpParser, tmp_path, src, "wp-content/plugins/test.php")
    hooks = [s for s in rec.statements if s.semanticType == "route"]
    assert len(hooks) == 1
    assert hooks[0].endpoint is None


def test_any_route_method_across_frameworks(tmp_path: Path) -> None:
    # Laravel Route::any
    laravel_src = b"""<?php
Route::any('/all-verbs', 'TestController@handle');
"""
    rec_l = _parse(LaravelParser, tmp_path, laravel_src, "routes/web.php")
    routes_l = [s for s in rec_l.statements if s.semanticType == "route"]
    assert len(routes_l) == 1
    assert routes_l[0].method == "ANY"
    assert routes_l[0].routeKind == "route"

    # Slim $app->any
    slim_src = b"""<?php
$app->any('/all-verbs', function($req, $res) { return $res; });
"""
    rec_s = _parse(SlimParser, tmp_path, slim_src, "src/routes.php")
    routes_s = [s for s in rec_s.statements if s.semanticType == "route"]
    assert len(routes_s) == 1
    assert routes_s[0].method == "ANY"
    assert routes_s[0].routeKind == "route"

    # CodeIgniter $routes->add
    ci_src = b"""<?php
$routes->add('/all-verbs', 'TestController::index');
"""
    rec_c = _parse(CodeIgniterParser, tmp_path, ci_src, "app/Config/Routes.php")
    routes_c = [s for s in rec_c.statements if s.semanticType == "route"]
    assert len(routes_c) == 1
    assert routes_c[0].method == "ANY"
    assert routes_c[0].routeKind == "route"

    # Symfony #[Route('/health')]
    symfony_src = b"""<?php
namespace App\\Controller;

use Symfony\\Component\\Routing\\Attribute\\Route;

class HealthController
{
    #[Route('/health')]
    public function health() {}
}
"""
    rec_sym = _parse(SymfonyParser, tmp_path, symfony_src, "src/Controller/HealthController.php")
    routes_sym = [s for s in rec_sym.statements if s.semanticType == "route"]
    assert len(routes_sym) == 1
    assert routes_sym[0].endpoint == "/health"
    assert routes_sym[0].method == "ANY"
    assert routes_sym[0].routeKind == "route"
    assert routes_sym[0].nodeType == "synthetic"


def test_codeigniter4_cli_route(tmp_path: Path) -> None:
    src = b"""<?php
namespace Config;

$routes->cli('cron/run', 'CronController::run');
"""
    rec = _parse(CodeIgniterParser, tmp_path, src, "app/Config/Routes.php")
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 1
    assert routes[0].endpoint == "cron/run"
    assert routes[0].method == "RPC"
    assert routes[0].routeKind == "route"


def test_no_duplicate_statements_wordpress_hook(tmp_path: Path) -> None:
    """§2.4 regression: add_action() must produce exactly ONE Statement per call.

    Before the fix, the base PHP parser emitted an ``expression_statement``
    node and the WordPress detector emitted a second ``function_call_expression``
    node for the same source span — two records, only one with semanticType set.
    After the fix the detector mutates the existing record in place, leaving
    exactly one Statement with semanticType='route'.
    """
    src = b"<?php\nadd_action('init', 'my_custom_init');\n"
    rec = _parse(PhpParser, tmp_path, src, "wp-content/plugins/test.php")

    # Exactly ONE statement total (not two for the same span).
    assert len(rec.statements) == 1, (
        f"Expected 1 Statement, got {len(rec.statements)}: "
        + str([s.model_dump(include={"nodeType", "semanticType", "endpoint"}) for s in rec.statements])
    )
    stmt = rec.statements[0]
    assert stmt.nodeType == "expression_statement"
    assert stmt.semanticType == "route"
    assert stmt.routeKind == "eventbus_consumer"
    assert stmt.endpoint == "init"
    assert stmt.method == "CONSUMER"


def test_no_duplicate_statements_laravel_route(tmp_path: Path) -> None:
    """§2.4 regression: Route::get() must produce exactly ONE Statement per call."""
    src = b"<?php\nuse Illuminate\\Support\\Facades\\Route;\nRoute::get('/ping', 'PingController@index');\n"
    rec = _parse(LaravelParser, tmp_path, src, "routes/web.php")

    ping_stmts = [
        s for s in rec.statements if s.semanticType == "route" and s.endpoint == "/ping"
    ]
    assert len(ping_stmts) == 1, (
        f"Expected 1 Statement for /ping, got {len(ping_stmts)}: "
        + str([s.model_dump(include={"nodeType", "semanticType", "endpoint"}) for s in ping_stmts])
    )
    stmt = ping_stmts[0]
    assert stmt.nodeType == "expression_statement"
    assert stmt.method == "GET"


def test_no_duplicate_statements_slim_route(tmp_path: Path) -> None:
    """§2.4 regression: $app->get() must produce exactly ONE Statement per call."""
    src = b"<?php\nuse Slim\\Factory\\AppFactory;\n$app = AppFactory::create();\n$app->get('/ping', function ($req, $res) { return $res; });\n"
    rec = _parse(SlimParser, tmp_path, src, "src/index.php")

    ping_stmts = [
        s for s in rec.statements if s.semanticType == "route" and s.endpoint == "/ping"
    ]
    assert len(ping_stmts) == 1, (
        f"Expected 1 Statement for /ping, got {len(ping_stmts)}: "
        + str([s.model_dump(include={"nodeType", "semanticType", "endpoint"}) for s in ping_stmts])
    )
    stmt = ping_stmts[0]
    assert stmt.nodeType == "expression_statement"
    assert stmt.method == "GET"


def test_no_duplicate_statements_codeigniter_route(tmp_path: Path) -> None:
    """§2.4 regression: $routes->get() must produce exactly ONE Statement per call."""
    src = b"<?php\n$routes->get('/ping', 'PingController::index');\n"
    rec = _parse(CodeIgniterParser, tmp_path, src, "app/Config/Routes.php")

    ping_stmts = [
        s for s in rec.statements if s.semanticType == "route" and s.endpoint == "/ping"
    ]
    assert len(ping_stmts) == 1, (
        f"Expected 1 Statement for /ping, got {len(ping_stmts)}: "
        + str([s.model_dump(include={"nodeType", "semanticType", "endpoint"}) for s in ping_stmts])
    )
    stmt = ping_stmts[0]
    assert stmt.nodeType == "expression_statement"
    assert stmt.method == "GET"



# ---------------------------------------------------------------------------
# parentId fixture tests -- route inside named method -> parentId = method id
# ---------------------------------------------------------------------------

def test_laravel_route_parentid_inside_method(tmp_path):
    """Route::get() inside a class method must have parentId = method's function_id, not file_id."""
    src = (
        b'<?php\n'
        b'namespace App\\Providers;\n'
        b'\n'
        b'use Illuminate\\Support\\Facades\\Route;\n'
        b'\n'
        b'class RouteServiceProvider\n'
        b'{\n'
        b'    public function map()\n'
        b'    {\n'
        b"        Route::get('/users', 'UserController@index');\n"
        b'    }\n'
        b'}\n'
    )
    path = "app/Providers/RouteServiceProvider.php"
    rec = _parse(LaravelParser, tmp_path, src, path)

    route = next((s for s in rec.statements if s.semanticType == "route" and s.endpoint == "/users"), None)
    assert route is not None, "Expected a route statement for /users"

    # Route::get() is inside map() which starts at line 8 inside RouteServiceProvider.
    expected_parent = function_id(path, "map", 8, class_name="RouteServiceProvider")
    assert route.parentId == expected_parent, (
        f"Expected parentId={expected_parent!r}, got {route.parentId!r}"
    )


def test_slim_route_parentid_inside_function(tmp_path):
    """$app->get() inside a named top-level function must have parentId = that function's id."""
    src = (
        b'<?php\n'
        b'use Slim\\Factory\\AppFactory;\n'
        b'\n'
        b'function register_routes($app) {\n'
        b"    $app->get('/ping', function ($req, $res) { return $res; });\n"
        b'}\n'
    )
    path = "src/routes.php"
    rec = _parse(SlimParser, tmp_path, src, path)

    route = next((s for s in rec.statements if s.semanticType == "route" and s.endpoint == "/ping"), None)
    assert route is not None, "Expected a route statement for /ping"

    # register_routes() is a top-level function starting at line 4 (no class_name).
    expected_parent = function_id(path, "register_routes", 4)
    assert route.parentId == expected_parent, (
        f"Expected parentId={expected_parent!r}, got {route.parentId!r}"
    )


def test_codeigniter_route_parentid_inside_function(tmp_path):
    """$routes->get() inside a named function must have parentId = that function's id."""
    src = (
        b'<?php\n'
        b'namespace Config;\n'
        b'\n'
        b'function load_routes($routes) {\n'
        b"    $routes->get('/api/users', 'UserController::index');\n"
        b'}\n'
    )
    path = "app/Config/Routes.php"
    rec = _parse(CodeIgniterParser, tmp_path, src, path)

    route = next((s for s in rec.statements if s.semanticType == "route" and s.endpoint == "/api/users"), None)
    assert route is not None, "Expected a route statement for /api/users"

    # load_routes() starts at line 4.
    expected_parent = function_id(path, "load_routes", 4)
    assert route.parentId == expected_parent, (
        f"Expected parentId={expected_parent!r}, got {route.parentId!r}"
    )


def test_wordpress_hook_parentid_inside_method(tmp_path):
    """add_action() inside a class method must have parentId = that method's id."""
    src = (
        b'<?php\n'
        b'class MyPlugin\n'
        b'{\n'
        b'    public function register()\n'
        b'    {\n'
        b"        add_action('init', 'my_custom_init');\n"
        b'    }\n'
        b'}\n'
    )
    path = "wp-content/plugins/my-plugin/my-plugin.php"
    rec = _parse(PhpParser, tmp_path, src, path)

    hook = next((s for s in rec.statements if s.semanticType == "route" and s.endpoint == "init"), None)
    assert hook is not None, "Expected a route/hook statement for 'init'"

    # register() method in MyPlugin starts at line 4.
    expected_parent = function_id(path, "register", 4, class_name="MyPlugin")
    assert hook.parentId == expected_parent, (
        f"Expected parentId={expected_parent!r}, got {hook.parentId!r}"
    )

# ---------------------------------------------------------------------------
# requestDTO / responseDTO fixture tests (spec §2.5)
# ---------------------------------------------------------------------------

def test_symfony_map_request_payload_and_return_dto(tmp_path: Path) -> None:
    """Symfony #[MapRequestPayload] and typed return resolve to FQCN requestDTO/responseDTO."""
    src = b"""<?php
namespace App\\Controller;

use App\\DTO\\CreateUserDTO;
use App\\DTO\\UserResponse;
use Symfony\\Component\\Routing\\Attribute\\Route;
use Symfony\\Component\\HttpKernel\\Attribute\\MapRequestPayload;

class UserController
{
    #[Route('/api/users', methods: ['POST'])]
    public function create(#[MapRequestPayload] CreateUserDTO $dto): UserResponse
    {
        return new UserResponse();
    }
}
"""
    rec = _parse(SymfonyParser, tmp_path, src, "src/Controller/UserController.php")
    routes = [s for s in rec.statements if s.semanticType == "route" and s.endpoint == "/api/users"]
    assert len(routes) == 1
    assert routes[0].requestDTO == "App\\DTO\\CreateUserDTO"
    assert routes[0].responseDTO == "App\\DTO\\UserResponse"


def test_symfony_form_request_convention_dto(tmp_path: Path) -> None:
    """Symfony DTO/Request parameter convention resolves to FQCN requestDTO."""
    src = b"""<?php
namespace App\\Controller;

use App\\Request\\RegisterUserRequest;
use Symfony\\Component\\HttpFoundation\\JsonResponse;
use Symfony\\Component\\Routing\\Attribute\\Route;

class AuthController
{
    #[Route('/auth/register', methods: ['POST'])]
    public function register(RegisterUserRequest $request): JsonResponse
    {
        return new JsonResponse();
    }
}
"""
    rec = _parse(SymfonyParser, tmp_path, src, "src/Controller/AuthController.php")
    routes = [s for s in rec.statements if s.semanticType == "route" and s.endpoint == "/auth/register"]
    assert len(routes) == 1
    assert routes[0].requestDTO == "App\\Request\\RegisterUserRequest"
    assert routes[0].responseDTO == "Symfony\\Component\\HttpFoundation\\JsonResponse"


def test_laravel_form_request_closure_route_dto(tmp_path: Path) -> None:
    """Laravel FormRequest closure parameter and return type resolve to FQCN requestDTO/responseDTO."""
    src = b"""<?php
namespace App\\Routes;

use App\\Http\\Requests\\StorePostRequest;
use App\\Http\\Resources\\PostResource;
use Illuminate\\Support\\Facades\\Route;

Route::post('/posts', function (StorePostRequest $request): PostResource {
    return new PostResource();
});
"""
    rec = _parse(LaravelParser, tmp_path, src, "routes/web.php")
    routes = [s for s in rec.statements if s.semanticType == "route" and s.endpoint == "/posts"]
    assert len(routes) == 1
    assert routes[0].requestDTO == "App\\Http\\Requests\\StorePostRequest"
    assert routes[0].responseDTO == "App\\Http\\Resources\\PostResource"


def test_laravel_form_request_controller_route_dto(tmp_path: Path) -> None:
    """Laravel controller action type-hinted with FormRequest resolves to FQCN requestDTO/responseDTO."""
    src = b"""<?php
namespace App\\Http\\Controllers;

use App\\Http\\Requests\\UpdateProfileRequest;
use App\\Http\\Resources\\UserResource;
use Illuminate\\Support\\Facades\\Route;

class ProfileController
{
    public function update(UpdateProfileRequest $request): UserResource
    {
        return new UserResource();
    }
}

Route::put('/profile', [ProfileController::class, 'update']);
"""
    rec = _parse(LaravelParser, tmp_path, src, "routes/web.php")
    routes = [s for s in rec.statements if s.semanticType == "route" and s.endpoint == "/profile"]
    assert len(routes) == 1
    assert routes[0].requestDTO == "App\\Http\\Requests\\UpdateProfileRequest"
    assert routes[0].responseDTO == "App\\Http\\Resources\\UserResource"


def test_symfony_route_text_is_actual_source(tmp_path: Path) -> None:
    """Statement.text must be the actual attribute source, not a synthesized subset."""
    src = b"""\
<?php
namespace App\\Controller;

use Symfony\\Component\\Routing\\Attribute\\Route;

class DemoController
{
    #[Route('/x', methods: ['GET', 'POST'], name: 'x_route')]
    public function handle() {}
}
"""
    rec = _parse(SymfonyParser, tmp_path, src, "src/Controller/DemoController.php")
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) >= 1
    # Every emitted route's text must contain the full attribute source
    for route in routes:
        assert "methods:" in route.text, f"Expected real source with methods:, got: {route.text}"
        assert "name:" in route.text, f"Expected real source with name:, got: {route.text}"
        assert "x_route" in route.text, f"Expected real source with x_route, got: {route.text}"
        # Must NOT be the old synthesized form that only had the path
        assert route.text != "#[Route('/x')]", "text must not be the synthesized path-only form"



def test_slim_claims_via_composer_json_outside_cwd(tmp_path: Path, monkeypatch) -> None:
    """Slim app with composer.json declaring slim/slim and route registrations referencing $app
    without literal 'Slim\\App' string, run with CWD outside the repo root, MUST claim slim and capture routes.
    """
    repo = tmp_path / "app_repo"
    repo.mkdir()
    (repo / "composer.json").write_text(json.dumps({"require": {"slim/slim": "^4.0"}}))

    src = b"""<?php
$app->get('/api/users', function ($req, $res) { return $res; });
$app->post('/api/users', function ($req, $res) { return $res; });
"""
    rel_path = "public/index.php"
    abs_file = repo / rel_path
    abs_file.parent.mkdir(parents=True, exist_ok=True)
    abs_file.write_bytes(src)

    # Change working directory to a separate dir outside repo root
    outside_dir = tmp_path / "outside_cwd"
    outside_dir.mkdir()
    monkeypatch.chdir(outside_dir)

    slim_parser = SlimParser()
    assert slim_parser.claims(rel_path, src, repo_root=repo) is True

    registry.discover_builtin()
    selected = registry.select(rel_path, src, repo_root=repo)
    assert selected is not None
    assert selected.name == "php-slim"

    ctx = ParseContext(
        path=rel_path,
        abs_path=abs_file,
        source=src,
        repo_root=repo,
        capture_statements=True,
    )
    rec = selected.parse_file(ctx)
    assert rec.framework == "slim"

    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 2
    endpoints = {r.endpoint: r for r in routes}
    assert "/api/users" in endpoints
