"""PHP framework tests: Laravel, Slim, Symfony, WordPress, Eloquent, Doctrine, PDO, WPDB, Guzzle."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from breezeai_cog.emit import to_line
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
    assert len(routes) >= 4

    endpoints = {r.endpoint: r for r in routes}
    assert "/users" in endpoints
    assert endpoints["/users"].method in ("GET", "POST")
    assert "/profile" in endpoints
    assert "photos" in endpoints


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
    assert len(routes) >= 3
    endpoints = {r.endpoint: r for r in routes}
    assert "/api/users" in endpoints
    assert "/api/items" in endpoints


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
    assert len(routes) >= 2
    endpoints = {r.endpoint for r in routes}
    assert "/api/users" in endpoints
    assert "/api/users/{id}" in endpoints


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
    assert len(routes) >= 5

    endpoints = {r.endpoint: r for r in routes}
    assert "/users" in endpoints
    assert "/profile" in endpoints
    assert "photos" in endpoints
    assert "/admin/dashboard" in endpoints


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
    endpoints = {r.endpoint: r for r in routes}

    assert "default_controller" not in endpoints
    assert "404_override" not in endpoints
    assert "journals" in endpoints
    assert endpoints["journals"].method == "ALL"
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
    hooks = [s for s in rec.statements if s.semanticType == "route" and s.routeKind == "hook"]
    assert len(hooks) == 2
    tags = {h.endpoint for h in hooks}
    assert "init" in tags
    assert "the_content" in tags


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
