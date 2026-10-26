from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.php_laravel.parser import LaravelParser
from breezeai_cog.parsers.contract import validate


def _parse(source: str):
    return LaravelParser().parse_file(ParseContext(
        path="routes/web.php", abs_path=Path("routes/web.php"),
        source=source.encode(), repo_root=Path("."), capture_statements=True,
    ))


def test_laravel_role_table_contract() -> None:
    record = _parse("""<?php
use Illuminate\\Support\\Facades\\{Route, Event};
Route::view('/welcome', 'welcome');
Route::redirect('/old', '/new');
Route::resource('photos', PhotoController::class);
Event::dispatch('order.shipped');
""")
    routes = [s for s in record.statements if s.semanticType == "route"]
    assert not any(v.severity == "error" for v in validate(record))
    assert {(s.method, s.routeKind, s.handler) for s in routes if s.endpoint in {'/welcome', '/old'}} == {
        ("GET", "route", None),
    }
    assert any(s.handler == "PhotoController@show" for s in routes)
    assert next(s for s in record.statements if s.semanticType == "eventbus_publish").method is None
