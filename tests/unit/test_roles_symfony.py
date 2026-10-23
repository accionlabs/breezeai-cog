from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.contract import validate
from breezeai_cog.parsers.php_symfony.parser import SymfonyParser


def test_symfony_role_table_contract() -> None:
    source = b"""<?php
use Symfony\\Component\\Routing\\Attribute\\Route;
class C { #[Route('/x')] public function x() {} }
"""
    record = SymfonyParser().parse_file(ParseContext(
        path="src/C.php", abs_path=Path("src/C.php"), source=source,
        repo_root=Path("."), capture_statements=True,
    ))
    [route] = [s for s in record.statements if s.semanticType == "route"]
    assert not any(v.severity == "error" for v in validate(record))
    assert (route.method, route.routeKind, route.nodeType) == ("ANY", "route", "attribute_group")
