"""build_index: tsconfig path-alias resolution + pipeline wiring."""

from __future__ import annotations

import pickle

from breezeai_cog import iter_file_records
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript.imports import TsAliasIndex
from breezeai_cog.parsers.typescript.parser import TypeScriptParser


def _repo(tmp_path) -> None:
    (tmp_path / "tsconfig.json").write_text(
        '{\n  // comment\n  "compilerOptions": {\n'
        '    "baseUrl": ".",\n    "paths": { "@app/*": ["src/app/*"] },\n  },\n}\n'
    )
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "foo.ts").write_text("export const foo = 1;\n")
    (tmp_path / "main.ts").write_text("import { foo } from '@app/foo';\nimport axios from 'axios';\n")


def test_build_index_reads_tsconfig(tmp_path) -> None:
    _repo(tmp_path)
    index = TypeScriptParser().build_index(tmp_path, [tmp_path / "main.ts"])
    assert isinstance(index, TsAliasIndex)
    assert index.paths == {"@app/*": ["src/app/*"]}
    pickle.loads(pickle.dumps(index))  # must cross the process boundary


def test_alias_resolution_with_index(tmp_path) -> None:
    _repo(tmp_path)
    parser = TypeScriptParser()
    index = parser.build_index(tmp_path, [])
    src = (tmp_path / "main.ts").read_bytes()
    ctx = ParseContext(path="main.ts", abs_path=tmp_path / "main.ts", source=src,
                       repo_root=tmp_path, resolution_index=index)
    rec = parser.parse_file(ctx)
    assert any(p.endswith("src/app/foo.ts") for p in rec.importFiles)  # alias -> in-repo file
    assert "axios" in rec.externalImports


def test_alias_unresolved_without_index(tmp_path) -> None:
    _repo(tmp_path)
    parser = TypeScriptParser()
    src = (tmp_path / "main.ts").read_bytes()
    ctx = ParseContext(path="main.ts", abs_path=tmp_path / "main.ts", source=src,
                       repo_root=tmp_path, resolution_index=None)
    rec = parser.parse_file(ctx)
    assert "@app/foo" in rec.externalImports  # no index -> stays external


def test_pipeline_wires_build_index(tmp_path) -> None:
    _repo(tmp_path)
    records = {r.path: r for r in iter_file_records(tmp_path)}
    assert any(p.endswith("src/app/foo.ts") for p in records["main.ts"].importFiles)
