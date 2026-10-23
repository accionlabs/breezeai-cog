from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.contract import validate
from breezeai_cog.parsers.php_codeigniter.parser import CodeIgniterParser


def test_codeigniter_role_table_contract() -> None:
    record = CodeIgniterParser().parse_file(ParseContext(
        path="app/Config/Routes.php", abs_path=Path("app/Config/Routes.php"),
        source=b"<?php $routes->view('welcome', 'welcome'); $routes->addRedirect('old', 'new'); $routes->cli('job', 'Job::run');",
        repo_root=Path("."), capture_statements=True,
    ))
    routes = [s for s in record.statements if s.semanticType == "route"]
    assert not any(v.severity == "error" for v in validate(record))
    assert {(s.method, s.endpoint, s.handler) for s in routes} == {
        ("GET", "/welcome", None), ("GET", "/old", None),
    }
    assert not any(s.method == "CLI" for s in routes)
