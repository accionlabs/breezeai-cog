"""A non-relative import resolves under the nearest tsconfig's ``baseUrl`` when the file is there,
as TypeScript looks it up after ``paths`` (``typescript/imports.py``): ``from 'src/x'`` with
``"baseUrl": "./"``. A package import (``@nestjs/common``) is never looked for on disk."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog.parsers.typescript.imports import _resolve, build_alias_index


def _repo(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def _resolved(root: Path, module: str, from_file: str) -> str | None:
    return _resolve(module, from_file, root, build_alias_index(root))


@pytest.mark.parametrize(
    "tsconfig, module, expected",
    [
        ('{"compilerOptions": {"baseUrl": "./"}}', "src/hooks/topics", "src/hooks/topics.ts"),
        ('{"compilerOptions": {"baseUrl": "./src"}}', "hooks/topics", "src/hooks/topics.ts"),
        ('{"compilerOptions": {"baseUrl": "./"}}', "src/hooks", "src/hooks/index.ts"),
        ('{"compilerOptions": {"baseUrl": "./"}}', "src/missing", None),  # no such file
        ('{"compilerOptions": {"baseUrl": "./"}}', "@nestjs/common", None),  # a package
        ('{"compilerOptions": {}}', "src/hooks/topics", None),  # no baseUrl: not looked up
    ],
)
def test_a_non_relative_import_resolves_under_base_url(tmp_path, tsconfig, module, expected) -> None:
    _repo(tmp_path, {
        "tsconfig.json": tsconfig,
        "src/hooks/topics.ts": "export const T = 'x';\n",
        "src/hooks/index.ts": "",
        "src/app/a.ts": "",
    })
    assert _resolved(tmp_path, module, "src/app/a.ts") == expected


def test_paths_win_over_base_url(tmp_path) -> None:
    _repo(tmp_path, {
        "tsconfig.json": '{"compilerOptions": {"baseUrl": "./", "paths": {"src/*": ["lib/*"]}}}',
        "src/x.ts": "", "lib/x.ts": "", "src/app/a.ts": "",
    })
    assert _resolved(tmp_path, "src/x", "src/app/a.ts") == "lib/x.ts"


def test_base_url_inherited_through_extends_is_relative_to_its_config(tmp_path) -> None:
    _repo(tmp_path, {
        "tsconfig.base.json": '{"compilerOptions": {"baseUrl": "./packages/api"}}',
        "packages/api/tsconfig.json": '{"extends": "../../tsconfig.base.json"}',
        "packages/api/src/topics.ts": "", "packages/api/src/a.ts": "",
    })
    assert _resolved(tmp_path, "src/topics", "packages/api/src/a.ts") == "packages/api/src/topics.ts"


def test_a_base_url_outside_the_repository_is_not_used(tmp_path) -> None:
    repo = _repo(tmp_path / "repo", {
        "tsconfig.json": '{"compilerOptions": {"baseUrl": "../shared"}}', "src/a.ts": "",
    })
    _repo(tmp_path / "shared", {"util.ts": ""})
    assert _resolved(repo, "util", "src/a.ts") is None


def test_a_nest_topic_imported_through_base_url_is_folded(analyze_files) -> None:
    st = analyze_files({
        "package.json": '{"dependencies": {"@nestjs/common": "10", "@nestjs/microservices": "10"}}',
        "tsconfig.json": '{"compilerOptions": {"baseUrl": "./"}}',
        "src/producer/topics.ts": "export const CONTACT_TOPIC = 'contact-events';\n",
        "src/hubspot/hubspot.controller.ts": "import { Controller } from '@nestjs/common';\n"
        "import { EventPattern } from '@nestjs/microservices';\n"
        "import { CONTACT_TOPIC } from 'src/producer/topics';\n"
        "@Controller()\nexport class C {\n  @EventPattern(CONTACT_TOPIC)\n  handle() {}\n}\n",
    })
    found = [s.endpoint for s in st if s.semanticType == "eventbus_consumer"]
    assert found == ["contact-events"]
