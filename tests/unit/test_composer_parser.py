"""Composer metadata extractor tests."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.structured_json.parser import JsonParser

COMPOSER_JSON = b"""{
    "name": "laravel/laravel",
    "type": "project",
    "description": "The skeleton application for the Laravel framework.",
    "require": {
        "php": "^8.2",
        "laravel/framework": "^11.0",
        "guzzlehttp/guzzle": "^7.8"
    },
    "require-dev": {
        "phpunit/phpunit": "^11.0",
        "mockery/mockery": "^1.6"
    },
    "autoload": {
        "psr-4": {
            "App\\\\": "app/"
        }
    }
}"""


def test_composer_json_extraction(tmp_path: Path) -> None:
    p = tmp_path / "composer.json"
    p.write_bytes(COMPOSER_JSON)
    parser = JsonParser()
    ctx = ParseContext(
        path="composer.json",
        abs_path=p,
        source=COMPOSER_JSON,
        repo_root=tmp_path,
    )
    rec = parser.parse_file(ctx)
    assert rec.type == "config"
    assert rec.language == "config"
    meta = rec.metadata or {}
    assert meta.get("kind") == "composer"
    assert meta.get("packageManager") == "composer"
    pkg = meta.get("packageInfo") or {}
    assert pkg.get("name") == "laravel/laravel"
    assert pkg.get("type") == "project"
    deps = pkg.get("dependencies") or {}
    assert "laravel/framework" in deps
    assert "guzzlehttp/guzzle" in deps
    dev_deps = pkg.get("devDependencies") or {}
    assert "phpunit/phpunit" in dev_deps
    assert meta.get("dependencyCount") == 3
    assert meta.get("devDependencyCount") == 2
