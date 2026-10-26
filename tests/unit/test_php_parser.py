"""PHP language and framework parser coverage."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.core import registry
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.php.index import PhpIndex
from breezeai_cog.parsers.php.parser import PhpParser
from breezeai_cog.parsers.php_codeigniter.parser import CodeIgniterParser
from breezeai_cog.parsers.php_laravel.parser import LaravelParser
from breezeai_cog.parsers.php_slim.parser import SlimParser
from breezeai_cog.parsers.php_symfony.parser import SymfonyParser
from breezeai_cog.parsers.php_twig.parser import TwigParser, TwigTemplateIndex
from breezeai_cog.schemas import FileRecord


def _context(source: str, *, path: str = "src/Controller.php", index=None,
             capture: bool = True) -> ParseContext:
    return ParseContext(
        path=path,
        abs_path=Path(path),
        source=source.encode(),
        repo_root=Path("."),
        capture_statements=capture,
        resolution_index=index,
    )


def _parse(parser, source: str, **kwargs) -> FileRecord:
    return parser.parse_file(_context(source, **kwargs))


def test_php_extracts_classes_methods_properties_closures_and_imports() -> None:
    source = """<?php
namespace App\\Http;
use App\\Models\\{User, Post};
use function App\\Support\\helper;
class Controller extends BaseController {
    public const VERSION = '1';
    private ?string $name;
    public function index(Request $request): Response {
        $callback = function (User $user): void { $user->save(); };
        $arrow = fn(Post $post) => $post->getTitle();
    }
}
"""
    index = PhpIndex(fqcn={
        "App\\Models\\User": "app/Models/User.php",
        "App\\Models\\Post": "app/Models/Post.php",
        "App\\Http\\Controller": "src/Controller.php",
    })
    record = _parse(PhpParser(), source, index=index)

    assert record.language == "php"
    assert record.importFiles == ["app/Models/User.php", "app/Models/Post.php"]
    assert record.externalImports == ["function App\\Support\\helper"]
    assert [(cls.name, cls.type, cls.extends) for cls in record.classes] == [
        ("Controller", "class", "BaseController")
    ]
    assert [fn.type for fn in record.functions] == [
        "method", "function_expression", "arrow_function",
    ]
    assert [fn.visibility for fn in record.functions] == ["public", None, None]
    assert [statement.name for statement in record.statements
            if statement.nodeType in {"property_declaration", "const_declaration"}] == [
        "VERSION", "name",
    ]
    assert [(p["name"], p["visibility"], p["isConst"], p["type"]) for p in record.classes[0].properties] == [
        ("VERSION", "public", True, None),
        ("name", "private", False, "?string"),
    ]
    assert all(statement.parentId for statement in record.statements)
    assert FileRecord.model_validate(record.model_dump(by_alias=True))


def test_php_links_only_includes_anchored_to_the_file(tmp_path: Path) -> None:
    (tmp_path / "lib").mkdir()
    for name in ("lib/a.php", "lib/b.php", "c.php", "d.php", "lib/bare.php"):
        (tmp_path / name).write_text("<?php\n")
    source = (
        b"<?php require __DIR__ . '/a.php'; include_once(dirname(__FILE__) . \"/b.php\");"
        b" require_once dirname(__DIR__) . '/c.php'; include dirname(__FILE__, 2) . '/d.php';"
        b" include 'bare.php'; require $base . '/a.php'; require __DIR__ . '/missing.php';"
        b" require __DIR__ . '/../../outside.php';"
    )
    context = ParseContext(
        path="lib/main.php", abs_path=tmp_path / "lib/main.php", source=source,
        repo_root=tmp_path, capture_statements=False,
    )
    record = PhpParser().parse_file(context)
    assert record.importFiles == ["lib/a.php", "lib/b.php", "c.php", "d.php"]


def test_php_resolves_direct_and_grouped_class_imports_and_captures_constructor_params() -> None:
    source = """<?php
namespace App\\Http;
use App\\Models\\User as AccountUser, App\\Models\\Post;
use Vendor\\Sdk\\{Client, MissingClient as MissingAlias, function run as runHelper, const VERSION as SDK_VERSION};
class Controller {
    public function __construct(private AccountUser $user, ?Client $client = null) {}
}
"""
    index = PhpIndex(fqcn={
        "app\\Models\\User": "app/Models/User.php",
        "App\\Models\\Post": "app/Models/Post.php",
        "Vendor\\Sdk\\Client": "vendor/Sdk/Client.php",
    })
    record = _parse(PhpParser(), source, index=index)

    assert record.importFiles == [
        "app/Models/User.php",
        "app/Models/Post.php",
        "vendor/Sdk/Client.php",
    ]
    assert record.externalImports == [
        "Vendor\\Sdk\\MissingClient",
        "function Vendor\\Sdk\\run",
        "const Vendor\\Sdk\\VERSION",
    ]
    assert [(param.name, param.type) for param in record.classes[0].constructorParams] == [
        ("user", "AccountUser"),
        ("client", "Client"),
    ]


def test_php_framework_parsers_preserve_import_and_constructor_capture() -> None:
    source = """<?php
namespace App\\Controllers;
use App\\Models\\Employee;
class Employees {
    public function __construct(Employee $employee) {}
}
"""
    index = PhpIndex(fqcn={"App\\Models\\Employee": "app/Models/Employee.php"})

    expected_frameworks = [
        (PhpParser(), None),
        (CodeIgniterParser(), "codeigniter"),
        (LaravelParser(), "laravel"),
        (SlimParser(), "slim"),
        (SymfonyParser(), "symfony"),
    ]

    for parser, expected_framework in expected_frameworks:
        record = _parse(parser, source, index=index)
        assert record.framework == expected_framework
        assert record.importFiles == ["app/Models/Employee.php"]
        assert [(param.name, param.type) for param in record.classes[0].constructorParams] == [
            ("employee", "Employee")
        ]


def test_framework_claims_do_not_steal_shared_php_dependencies() -> None:
    registry.discover_builtin()
    laravel_controller = b"""<?php
use Illuminate\\Support\\Facades\\Event;
use Symfony\\Component\\HttpFoundation\\Response;
Event::dispatch('order.shipped');
"""
    assert registry.select("app/Http/OrdersController.php", laravel_controller).name == "php-laravel"
    record = _parse(LaravelParser(), laravel_controller.decode(), path="app/Http/OrdersController.php")
    assert record.framework == "laravel"
    assert any(s.semanticType == "eventbus_publish" and s.endpoint == "order.shipped"
               for s in record.statements)

    unrelated_index = b"<?php\n$app->get('/x', 'SomeController::show');\n"
    unrelated_routes = b"<?php\n$routes = new RouteRegistry();\n$routes->get('/x', 'SomeController::show');\n"
    assert registry.select("public/index.php", unrelated_index).name == "php"
    assert registry.select("routes.php", unrelated_index).name == "php"
    assert registry.select("routes.php", unrelated_routes).name == "php-codeigniter"


def test_php_laravel_routes_capture_match_methods_and_prefix() -> None:
    source = """<?php
use Illuminate\\Support\\Facades\\Route;
Route::prefix('/admin')->group(function () {
    Route::match(['GET', 'POST'], '/users', [UserController::class, 'show']);
});
"""
    record = _parse(LaravelParser(), source, path="routes/web.php")
    routes = [s for s in record.statements if s.semanticType == "route"]

    assert record.framework == "laravel"
    assert [(r.method, r.endpoint, r.handler, r.routeKind, r.nodeType) for r in routes] == [
        ("GET", "/admin/users", "UserController@show", "route", "expression_statement"),
        ("POST", "/admin/users", "UserController@show", "route", "expression_statement"),
    ]
    assert len(record.statements) == 3
    assert sum(statement.semanticType is None for statement in record.statements) == 1


def test_php_laravel_routes_capture_name_and_chained_middleware() -> None:
    source = """<?php
use Illuminate\\Support\\Facades\\Route;
Route::get('/admin', [AdminController::class, 'index'])
    ->name('admin.index')
    ->middleware(['auth', AdminMiddleware::class]);
"""
    record = _parse(LaravelParser(), source, path="routes/web.php")
    routes = [s for s in record.statements if s.semanticType == "route"]

    assert len(routes) == 1
    assert routes[0].name == "admin.index"
    assert routes[0].guards == ["auth", "AdminMiddleware"]


def test_php_laravel_route_groups_capture_name_and_middleware_prefixes() -> None:
    source = """<?php
use Illuminate\\Support\\Facades\\Route;
Route::name('admin.')->middleware('auth')->prefix('/admin')->group(function () {
    Route::get('/users', [AdminController::class, 'users'])->name('users.index');
});
"""
    record = _parse(LaravelParser(), source, path="routes/web.php")
    routes = [s for s in record.statements if s.semanticType == "route"]

    assert len(routes) == 1
    assert routes[0].endpoint == "/admin/users"
    assert routes[0].name == "admin.users.index"
    assert routes[0].guards == ["auth"]


def test_codeigniter_three_argument_group_retains_nested_route_and_filter() -> None:
    source = """<?php
use CodeIgniter\\Router\\RouteCollection;
$routes->group('admin', ['filter' => 'auth'], function ($routes) {
    $routes->get('users', 'AdminController::index');
});
"""
    record = _parse(CodeIgniterParser(), source, path="app/Config/Routes.php")
    routes = [s for s in record.statements if s.semanticType == "route"]

    assert len(routes) == 1
    assert routes[0].endpoint == "/admin/users"
    assert routes[0].handler == "AdminController@index"
    assert routes[0].guards == ["auth"]


def test_codeigniter_route_options_capture_name_and_filters() -> None:
    source = """<?php
use CodeIgniter\\Router\\RouteCollection;
$routes->get('users/(:num)', 'UserController::show/$1', [
    'as' => 'users.show',
    'filter' => ['auth', AuthFilter::class],
]);
"""
    record = _parse(CodeIgniterParser(), source, path="app/Config/Routes.php")
    routes = [s for s in record.statements if s.semanticType == "route"]

    assert len(routes) == 1
    assert routes[0].name == "users.show"
    assert routes[0].guards == ["auth", "AuthFilter"]


def test_slim_group_prefix_and_container_get_do_not_confuse_routes() -> None:
    source = """<?php
use Slim\\Factory\\AppFactory;
$app = AppFactory::create();
$app->get(SomeService::class);
$app->group('/api', function ($group) {
    $group->get('/hello/{name}', 'HelloAction');
});
"""
    record = _parse(SlimParser(), source, path="public/index.php")
    routes = [s for s in record.statements if s.semanticType == "route"]

    assert len(routes) == 1
    assert routes[0].endpoint == "/api/hello/{name}"
    assert routes[0].handler == "HelloAction"


def test_slim_routes_capture_name_and_chained_middleware() -> None:
    source = """<?php
use Slim\\Factory\\AppFactory;
$app = AppFactory::create();
$app->get('/admin', AdminController::class . ':index')
    ->setName('admin.index')
    ->add(AuthMiddleware::class)
    ->add(new AuditMiddleware());
"""
    record = _parse(SlimParser(), source, path="public/index.php")
    routes = [s for s in record.statements if s.semanticType == "route"]

    assert len(routes) == 1
    assert routes[0].name == "admin.index"
    assert routes[0].guards == ["AuthMiddleware", "AuditMiddleware"]


def test_symfony_attributes_use_ast_paths_and_any_default() -> None:
    source = """<?php
namespace App\\Controller;
use Symfony\\Component\\Routing\\Attribute\\Route;
class LegacyController {
    #[Route('/legacy:format', priority: 10)]
    public function show(): Response {}
}
"""
    record = _parse(SymfonyParser(), source, path="src/LegacyController.php")
    routes = [s for s in record.statements if s.semanticType == "route"]

    assert len(routes) == 1
    assert (routes[0].endpoint, routes[0].method, routes[0].routeKind) == (
        "/legacy:format", "ANY", "route",
    )
    assert routes[0].nodeType == "attribute_group"
    assert routes[0].text == "#[Route('/legacy:format', priority: 10)]"
    assert record.functions[0].decorators == []


def test_symfony_routes_capture_name_prefix_and_authorization_attributes() -> None:
    source = """<?php
namespace App\\Controller;
use Symfony\\Component\\Routing\\Attribute\\Route;
use Symfony\\Component\\Security\\Http\\Attribute\\IsGranted;
#[IsGranted('ROLE_ADMIN')]
#[Route('/admin', name: 'admin_')]
class AdminController {
    #[Route('/users', name: 'users.list', methods: ['GET'])]
    #[IsGranted('ROLE_USER')]
    public function users(): Response {}
}
"""
    record = _parse(SymfonyParser(), source, path="src/AdminController.php")
    routes = [s for s in record.statements if s.semanticType == "route"]

    assert len(routes) == 1
    assert routes[0].name == "admin_users.list"
    assert routes[0].guards == ["IsGranted:ROLE_ADMIN", "IsGranted:ROLE_USER"]
    assert routes[0].authRequired is True


def test_symfony_dto_types_are_only_emitted_when_indexed() -> None:
    source = """<?php
namespace App\\Controller;
use Symfony\\Component\\Routing\\Attribute\\Route;
use Symfony\\Component\\HttpKernel\\Attribute\\MapRequestPayload;
use App\\Dto\\CreateOrder;
use App\\Dto\\OrderView;
class OrderController {
    #[Route('/orders', methods: ['POST'])]
    public function create(#[MapRequestPayload] CreateOrder $order): OrderView {}
}
"""
    index = PhpIndex(fqcn={
        "App\\Dto\\CreateOrder": "src/Dto/CreateOrder.php",
        "App\\Dto\\OrderView": "src/Dto/OrderView.php",
    })
    record = _parse(SymfonyParser(), source, index=index)
    route = next(s for s in record.statements if s.semanticType == "route")
    assert route.requestDTO == "CreateOrder"
    assert route.responseDTO == "OrderView"

    unresolved = _parse(SymfonyParser(), source)
    route_unresolved = next(s for s in unresolved.statements if s.semanticType == "route")
    assert route_unresolved.requestDTO is None
    assert route_unresolved.responseDTO is None


def test_php_twig_templates_capture_ast_and_resolve_static_template_references() -> None:
    source = """{% extends 'layouts/app.twig' %}
{% block content %}
  <p>{{ content_title | default('Hello') }}</p>
  {% include 'partials/content.twig' %}
  {% from 'partials/forms.twig' import field %}
  {{ include('partials/aside.twig') }}
{% endblock %}
{# page comment #}
"""
    index = TwigTemplateIndex(frozenset({
        "resources/views/index.twig",
        "resources/views/layouts/app.twig",
        "resources/views/partials/content.twig",
        "resources/views/partials/forms.twig",
        "resources/views/partials/aside.twig",
    }))
    record = _parse(
        TwigParser(),
        source,
        path="resources/views/index.twig",
        index=index,
    )

    assert record.language == "twig"
    assert record.framework == "twig"
    assert record.uiRole == "template"
    assert record.importFiles == [
        "resources/views/layouts/app.twig",
        "resources/views/partials/content.twig",
        "resources/views/partials/forms.twig",
        "resources/views/partials/aside.twig",
    ]
    assert {"tag_statement", "output_directive", "comment"} <= {
        statement.nodeType for statement in record.statements
    }
    assert any(statement.name == "extends" for statement in record.statements)
    assert any(statement.name == "content" for statement in record.statements)
    assert all(statement.semanticType is None for statement in record.statements)
    assert FileRecord.model_validate(record.model_dump(by_alias=True))


def test_php_twig_is_registered_and_template_capture_respects_statement_gate() -> None:
    registry.discover_builtin()
    parser = registry.select("resources/views/index.twig", b"{% block body %}Hello{% endblock %}")

    assert parser is not None
    assert parser.name == "twig"
    assert ".twig" in parser.template_extensions
    assert not _parse(
        parser,
        "{% block body %}Hello{% endblock %}",
        capture=False,
    ).statements


def test_all_php_framework_parser_claims_are_specific_and_blade_stays_unsupported() -> None:
    registry.discover_builtin()
    examples = {
        "php-laravel": (
            "use Illuminate\\Support\\Facades\\Route; Route::get('/x', 'X@y');",
            "routes/web.php",
        ),
        "php-codeigniter": (
            "use CodeIgniter\\Router\\RouteCollection; $routes->get('x', 'X::y');",
            "app/Config/Routes.php",
        ),
        "php-slim": (
            "use Slim\\Factory\\AppFactory; $app->get('/x', 'X');",
            "public/index.php",
        ),
        "php-symfony": (
            "use Symfony\\Component\\Routing\\Attribute\\Route; #[Route('/x')]",
            "src/X.php",
        ),
    }
    for expected, (source, path) in examples.items():
        assert registry.select(path, source.encode()).name == expected
    slim_entrypoint = b"<?php $app->get('/hello/{name}', $handler);"
    assert registry.select("sl/public/index.php", slim_entrypoint).name == "php"
    slim_delegated_entrypoint = b"<?php $app = init(); registerRoutes($app); $app->run();"
    assert registry.select("public/index.php", slim_delegated_entrypoint).name == "php"
    assert registry.select("app/Middlewares/Auth.php", b"<?php use Slim\\Psr7\\Response;").name == "php-slim"
    assert registry.select("app/Exceptions/H.php", b"<?php use Slim\\Exception\\HttpNotFoundException;").name == "php-slim"
    assert registry.select("templates/home.blade.php", b"<?php echo 'x';") is None
    assert PhpParser().matches("templates/home.blade.php") is False


def test_php_semantic_capture_obeys_statement_and_fixture_gates() -> None:
    source = """<?php
use Illuminate\\Support\\Facades\\Route;
Route::get('/users', 'UserController@index');
"""
    assert not any(
        statement.semanticType == "route"
        for statement in _parse(LaravelParser(), source, capture=False).statements
    )
    fixture = _parse(LaravelParser(), source, path="src/mock/UserController.php")
    assert not any(statement.semanticType == "route" for statement in fixture.statements)


def test_wordpress_hooks_are_event_consumers_not_routes() -> None:
    source = """<?php
class Plugin {
    public function register(): void {
        add_action('init', [$this, 'boot']);
        add_filter('the_content', 'filter_content');
        add_action($dynamic_hook, function () {});
    }
}
"""
    record = _parse(PhpParser(), source)
    hooks = [s for s in record.statements if s.semanticType == "eventbus_consumer"]

    assert [(s.endpoint, s.method, s.framework, s.handler, s.routeKind) for s in hooks] == [
        ("init", "CONSUMER", "wordpress", "boot", None),
        ("the_content", "CONSUMER", "wordpress", "filter_content", None),
    ]
    assert record.framework == "wordpress"
    assert not any(s.semanticType == "route" for s in record.statements)


def test_codeigniter_query_builder_db_method_calls() -> None:
    source = """<?php
namespace App\\Models;
class Item {
    public function get_items($id) {
        $builder = $this->db->table('items');
        $builder->where('item_id', $id);
        return $builder->get()->getResult();
    }
}
"""
    record = _parse(CodeIgniterParser(), source, path="app/Models/Item.php")
    db_calls = [s for s in record.statements if s.semanticType == "db_method_call"]
    assert not db_calls


def test_php_enum_members_are_captured_as_child_statements() -> None:
    record = _parse(PhpParser(), """<?php
enum FileSizeUnit: int {
    case B = 0;
    case KB = 1;
}
""")
    members = [s for s in record.statements if s.semanticType == "enum_member"]

    assert [(s.name, s.nodeType, s.text) for s in members] == [
        ("B", "enum_case", "case B = 0;"),
        ("KB", "enum_case", "case KB = 1;"),
    ]
    assert all(s.parentId == record.classes[0].id for s in members)


def test_codeigniter_events_service_routes_and_convenience_routes_are_semantic() -> None:
    record = _parse(CodeIgniterParser(), """<?php
use CodeIgniter\\Events\\Events;
Events::on('pre_system', 'Boot::run');
Events::trigger('cache.clear');
service('routes')->get('__hot-reload', 'HotReload::index');
$routes->resource('photos', ['only' => ['index', 'show']]);
""", path="app/Config/Routes.php")

    events = [s for s in record.statements if s.semanticType.startswith("eventbus_")]
    routes = [s for s in record.statements if s.semanticType == "route"]

    assert [(s.semanticType, s.endpoint, s.method) for s in events] == [
        ("eventbus_consumer", "pre_system", "CONSUMER"),
        ("eventbus_publish", "cache.clear", "PUBLISH"),
    ]
    assert ("GET", "/__hot-reload", "HotReload@index") in [
        (s.method, s.endpoint, s.handler) for s in routes
    ]
    assert {s.endpoint for s in routes if s.routeKind == "resource"} == {
        "/photos", "/photos/(:segment)",
    }


def test_codeigniter_claims_do_not_use_diagnostic_strings_and_repairs_php_db_hints() -> None:
    parser = CodeIgniterParser()
    assert not parser.claims(
        "structarmed-baseline.php",
        b"<?php return ['class' => 'CodeIgniter\\Config\\BaseService'];",
    )
    record = _parse(CodeIgniterParser(), """<?php
namespace CodeIgniter\\Database;
class BaseBuilder {
    public function save($data) { return $this->upsert($data); }
}
""", path="system/Database/BaseBuilder.php")
    upsert = [s for s in record.statements if s.method == "upsert"]
    assert not upsert


def test_route_handler_binary_concatenation_and_formats() -> None:
    source = """<?php
use Slim\\Factory\\AppFactory;
$app = AppFactory::create();
$app->get('/', HomeController::class . ':index');
$app->get('/status', ApiController::class . ':status');
$app->post('/users', [UserController::class, 'store']);
$app->get('/colon', 'OtherController:show');
$app->get('/invokable', SingleAction::class);
"""
    record = _parse(SlimParser(), source, path="routes/web.php")
    routes = [s for s in record.statements if s.semanticType == "route"]
    assert len(routes) == 5
    handlers = {r.endpoint: r.handler for r in routes}
    assert handlers["/"] == "HomeController@index"
    assert handlers["/status"] == "ApiController@status"
    assert handlers["/users"] == "UserController@store"
    assert handlers["/colon"] == "OtherController@show"
    assert handlers["/invokable"] == "SingleAction"


def test_laravel_special_routes_and_resources_use_entry_point_contract() -> None:
    source = """<?php
use Illuminate\\Support\\Facades\\Route;
Route::view('/welcome', 'welcome');
Route::redirect('/here', 'https://example.com/there');
Route::resource('photos', PhotoController::class);
Route::apiResource('albums', AlbumController::class);
Route::singleton('profile', ProfileController::class);
"""
    record = _parse(LaravelParser(), source, path="routes/web.php")
    routes = [s for s in record.statements if s.semanticType == "route"]

    assert all(route.routeKind == "route" for route in routes)
    assert not any(route.method in {"VIEW", "REDIRECT", "RESOURCE", "API_RESOURCE"} for route in routes)
    assert next(route for route in routes if route.endpoint == "/welcome").handler is None
    assert next(route for route in routes if route.endpoint == "/here").handler is None
    assert any(route.endpoint == "/photos/{id}" and route.method == "GET"
               and route.handler == "PhotoController@show" for route in routes)
    assert any(route.endpoint == "/albums/{id}" and route.method == "PATCH"
               and route.handler == "AlbumController@update" for route in routes)
    assert any(route.endpoint == "/profile" and route.method == "DELETE"
               and route.handler == "ProfileController@destroy" for route in routes)


def test_php_route_locations_and_dynamic_prefixes_are_honest() -> None:
    source = """<?php
use Illuminate\\Support\\Facades\\Route;
Route::prefix($dynamic)->group(function () {
    Route::get('/inner', [UserController::class, 'index']);
});
Route::get('/closure', function () {});
class UserController { public function index() {} }
"""
    record = _parse(LaravelParser(), source, path="routes/web.php")
    routes = [s for s in record.statements if s.semanticType == "route"]
    dynamic = next(route for route in routes if route.text.startswith("Route::get('/inner'"))
    closure = next(route for route in routes if route.endpoint == "/closure")

    assert dynamic.endpoint is None
    assert dynamic.handler == "UserController@index"
    assert dynamic.handlerLine is None
    assert closure.handler is None and closure.handlerLine is None


def test_slim_closures_and_codeigniter_contract_values() -> None:
    slim = _parse(SlimParser(), """<?php
use Slim\\App;
$app = new App();
$app->group($base, function ($group) { $group->get('/inner', function () {}); });
""", path="routes.php")
    slim_routes = [s for s in slim.statements if s.semanticType == "route"]
    assert slim_routes and slim_routes[0].endpoint is None
    assert slim_routes[0].handler is None
    assert slim_routes[0].handlerLine == 4

    ci = _parse(CodeIgniterParser(), """<?php
use CodeIgniter\\Router\\RouteCollection;
$routes->group($prefix, function () use ($routes) {
    $routes->get('users/(:alpha)', 'Users::show/$1');
});
$routes->cli('jobs', 'Jobs::run');
$routes->view('welcome', 'welcome');
$routes->addRedirect('here', 'there');
""", path="app/Config/Routes.php")
    ci_routes = [s for s in ci.statements if s.semanticType == "route"]
    assert len(ci_routes) == 3
    route = next(s for s in ci_routes if s.endpoint is None)
    assert route.handler == "Users@show"
    assert route.isRegex is True
    assert {(s.method, s.endpoint, s.handler) for s in ci_routes if s.endpoint is not None} == {
        ("GET", "/welcome", None), ("GET", "/here", None),
    }
    assert all(route.routeKind == "route" for route in ci_routes)


def test_php_double_quoted_literals_are_values_but_interpolation_is_not() -> None:
    record = _parse(LaravelParser(), """<?php
use Illuminate\\Support\\Facades\\Route;
Route::get("/users", [UserController::class, "index"]);
Route::get("/users/{$id}", [UserController::class, "show"]);
""")
    routes = [s for s in record.statements if s.semanticType == "route"]
    assert [(s.method, s.endpoint) for s in routes] == [("GET", "/users")]
