from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.contract import validate
from breezeai_cog.parsers.php.parser import PhpParser


def test_wordpress_role_table_contract() -> None:
    record = PhpParser().parse_file(ParseContext(
        path="plugin.php", abs_path=Path("plugin.php"),
        source=b"<?php add_action('init', 'boot'); add_filter('content', 'filter_content');",
        repo_root=Path("."), capture_statements=True,
    ))
    hooks = [s for s in record.statements if s.semanticType == "eventbus_consumer"]
    assert not any(v.severity == "error" for v in validate(record))
    assert [(s.method, s.endpoint) for s in hooks] == [
        ("CONSUMER", "init"), ("CONSUMER", "content"),
    ]
