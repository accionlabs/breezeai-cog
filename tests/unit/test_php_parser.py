"""PHP language parser unit tests + schema validation."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.php.parser import PhpParser
from breezeai_cog.schemas import ConstructorParam, FileRecord

SRC = b"""<?php
namespace App\\Models;

use App\\Services\\UserService;
use External\\Package\\Client;
use App\\Services\\OtherService as Other;

abstract class User extends Model implements Authenticatable, JsonSerializable
{
    private string $name;

    public function __construct(
        private UserService $service,
        public int $id,
        protected ?string $tag = null
    ) {}

    public static function findUser(int $id): User
    {
        return User::find($id);
    }
}

interface Authenticatable
{
}

trait Loggable
{
    public function log(string $msg): void {}
}

enum Status: string
{
    case ACTIVE = 'active';
    case INACTIVE = 'inactive';
}

function helper(string $name): string
{
    return $name;
}
"""
REL = "src/Models/User.php"


def _parse_php(
    tmp_path: Path, src: bytes = SRC, rel: str = REL, *, capture: bool = False
) -> FileRecord:
    svc_dir = tmp_path / "src/Services"
    svc_dir.mkdir(parents=True, exist_ok=True)
    (svc_dir / "UserService.php").write_text(
        "<?php\nnamespace App\\Services;\nclass UserService {}\n"
    )
    (svc_dir / "OtherService.php").write_text(
        "<?php\nnamespace App\\Services;\nclass OtherService {}\n"
    )

    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(src)

    parser = PhpParser()
    files = list(tmp_path.rglob("*.php")) + list(tmp_path.rglob("*.phtml"))
    index = parser.build_index(tmp_path, files)
    ctx = ParseContext(
        path=rel,
        abs_path=p,
        source=src,
        repo_root=tmp_path,
        resolution_index=index,
        capture_statements=capture,
    )
    return parser.parse_file(ctx)


def test_php_file_and_extensions(tmp_path: Path) -> None:
    rec_php = _parse_php(tmp_path, src=b"<?php echo 'hi';", rel="src/index.php")
    assert rec_php.language == "php"
    assert rec_php.type == "code"

    rec_phtml = _parse_php(tmp_path, src=b"<?php echo 'hi';", rel="templates/view.phtml")
    assert rec_phtml.language == "php"
    assert rec_phtml.type == "code"


def test_php_classes_and_types(tmp_path: Path) -> None:
    rec = _parse_php(tmp_path)
    by_name = {c.name: c for c in rec.classes}

    assert "User" in by_name
    assert "Authenticatable" in by_name
    assert "Loggable" in by_name
    assert "Status" in by_name

    user = by_name["User"]
    assert user.type == "class"
    assert user.isAbstract is True
    assert user.extends == "Model"
    assert "Authenticatable" in user.implements
    assert "JsonSerializable" in user.implements

    iface = by_name["Authenticatable"]
    assert iface.type == "interface"
    assert iface.isAbstract is True

    trait = by_name["Loggable"]
    assert trait.type == "trait"

    enum_cls = by_name["Status"]
    assert enum_cls.type == "enum"


def test_constructor_property_promotion(tmp_path: Path) -> None:
    rec = _parse_php(tmp_path)
    user = next(c for c in rec.classes if c.name == "User")
    assert user.constructorParams == [
        ConstructorParam(name="service", type="UserService"),
        ConstructorParam(name="id", type="int"),
        ConstructorParam(name="tag", type="?string"),
    ]


def test_functions_and_methods(tmp_path: Path) -> None:
    rec = _parse_php(tmp_path)
    by_name = {f.name: f for f in rec.functions}

    # Top-level helper function
    assert "helper" in by_name
    helper = by_name["helper"]
    assert helper.type == "function"
    assert helper.returnType == "string"
    assert len(helper.params) == 1
    assert helper.params[0].name == "name"
    assert helper.params[0].type == "string"
    assert helper.parentId == rec.id

    # Class methods
    user = next(c for c in rec.classes if c.name == "User")
    assert "__construct" in by_name
    ctor = by_name["__construct"]
    assert ctor.type == "constructor"
    assert ctor.visibility == "public"
    assert ctor.parentId == user.id

    assert "findUser" in by_name
    find_user = by_name["findUser"]
    assert find_user.type == "method"
    assert find_user.visibility == "public"
    assert find_user.isStatic is True
    assert find_user.returnType == "User"
    assert find_user.parentId == user.id


def test_imports_and_psr4_resolution(tmp_path: Path) -> None:
    rec = _parse_php(tmp_path)
    # Resolved in repo
    assert any("UserService.php" in f for f in rec.importFiles)
    assert any("OtherService.php" in f for f in rec.importFiles)
    # External
    assert "External\\Package\\Client" in rec.externalImports
    # Exports empty for PHP
    assert rec.exports == []


def test_comments_capture(tmp_path: Path) -> None:
    src_with_comments = b"""<?php
// Single line comment
# Shell style comment
/* Block comment */
/**
 * PHPDoc block
 */
function documented(): void {}
"""
    rec = _parse_php(tmp_path, src=src_with_comments, rel="src/comments.php", capture=True)
    comments = [s for s in rec.statements if s.semanticType == "comment"]
    assert len(comments) >= 1
    assert any("Single line comment" in c.text for c in comments)


def test_closure_whole_body_walk(tmp_path: Path) -> None:
    """Critical closure test: Route::get with anonymous closure containing $client->get."""
    src = b"""<?php
Route::get('/p', function () {
    $client->get('https://x');
});
"""
    rec = _parse_php(tmp_path, src=src, rel="routes/web.php", capture=True)
    # In base PHP or with statements captured:
    api_calls = [s for s in rec.statements if s.semanticType == "api_call"]
    assert len(api_calls) == 1
    assert api_calls[0].endpoint == "https://x"
    assert api_calls[0].method == "GET"


def test_schema_validity(tmp_path: Path) -> None:
    rec = _parse_php(tmp_path, capture=True)
    line = to_line(rec)
    data = json.loads(line)
    validator = Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
    errors = list(validator.iter_errors(data))
    assert not errors, [e.message for e in errors]


def test_order_service_fixture(tmp_path: Path) -> None:
    order_service_src = b"""<?php
declare(strict_types=1);

namespace App\\Services;

use App\\Models\\User;
use App\\Models\\Order;
use App\\Contracts\\PaymentGateway;
use Illuminate\\Database\\Eloquent\\Builder;
use Illuminate\\Support\\Collection;
use RuntimeException;

final class OrderService
{
    public function __construct(
        private readonly PaymentGateway $gateway,
        private readonly int $retryLimit = 3,
    ) {}

    public function processOrders(Collection $orders): Collection
    {
        return collect();
    }
}
"""
    rec = _parse_php(tmp_path, src=order_service_src, rel="src/Services/OrderService.php", capture=True)
    by_name = {c.name: c for c in rec.classes}
    assert "OrderService" in by_name
    order_svc = by_name["OrderService"]
    assert order_svc.type == "class"
    assert order_svc.constructorParams == [
        ConstructorParam(name="gateway", type="PaymentGateway"),
        ConstructorParam(name="retryLimit", type="int"),
    ]
    fn_names = {f.name for f in rec.functions}
    assert "__construct" in fn_names
    assert "processOrders" in fn_names

