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

    for parser in (
        PhpParser(),
        CodeIgniterParser(),
        LaravelParser(),
        SlimParser(),
        SymfonyParser(),
    ):
        record = _parse(parser, source, index=index)
        assert record.importFiles == ["app/Models/Employee.php"]
        assert [(param.name, param.type) for param in record.classes[0].constructorParams] == [
            ("employee", "Employee")
        ]


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
    assert routes[0].nodeType == "synthetic"
    assert routes[0].text == "#[Route('/legacy:format', priority: 10)]"
    assert record.functions[0].decorators == []


def test_symfony_dto_types_are_only_emitted_when_indexed() -> None:
    source = """<?php
namespace App\\Controller;
use Symfony\\Component\\Routing\\Attribute\\Route;
use Symfony\\Component\\HttpKernel\\Attribute\\MapRequestPayload;
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
    assert registry.select("sl/public/index.php", slim_entrypoint).name == "php-slim"
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
