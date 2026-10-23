from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.contract import validate
from breezeai_cog.parsers.php_slim.parser import SlimParser


def test_slim_role_table_contract() -> None:
    record = SlimParser().parse_file(ParseContext(
        path="routes.php", abs_path=Path("routes.php"),
        source=b"<?php use Slim\\App; $app->get('/x', function () {});",
        repo_root=Path("."), capture_statements=True,
    ))
    [route] = [s for s in record.statements if s.semanticType == "route"]
    assert not any(v.severity == "error" for v in validate(record))
    assert (route.method, route.endpoint, route.handler) == ("GET", "/x", None)
    assert route.handlerLine == 1
