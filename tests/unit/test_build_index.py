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
    # Aliases are scoped to the declaring tsconfig, with absolute targets.
    assert len(index.alias_scopes) == 1
    cfg_dir, paths = index.alias_scopes[0]
    assert cfg_dir == str(tmp_path.resolve())
    assert paths["@app/*"] == [str((tmp_path / "src" / "app").resolve()) + "/*"]
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


def test_const_object_flattened_to_dotted_values() -> None:
    # `flatten_const_object` (via _collect_const_values) records `X.a.b -> literal`, folding
    # templates, sibling flat consts, and object references — the reusable const-folding primitive.
    from breezeai_cog.parsers.treesitter import parse_source
    from breezeai_cog.parsers.typescript.imports import _collect_const_values

    src = (
        b"const seg = 'discover';\n"
        b"const tabs = { projects: 'projects' } as const;\n"
        b"export const paths = {\n"
        b"  root: `/${seg}`,\n"
        b"  discover: { tabs: tabs },\n"
        b"  dyn: someVar,\n"           # non-literal leaf -> not recorded (honest-null)
        b"} as const;\n"
    )
    cv: dict[str, str | None] = {}
    _collect_const_values(parse_source("typescript", src).root_node, src, cv)
    assert cv["paths.root"] == "/discover"                    # template + sibling flat const
    assert cv["paths.discover.tabs.projects"] == "projects"   # object-reference inline flatten
    assert "paths.dyn" not in cv                              # non-literal leaf dropped


def test_esm_js_specifier_resolves_to_ts_sibling(tmp_path) -> None:
    # ESM/NodeNext names the emitted `./x.js`; the source on disk is `x.ts`. The edge must
    # still resolve (importFiles), not leak into externalImports.
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "environment.ts").write_text("export const env = 1;\n")
    (tmp_path / "index.ts").write_text("import { env } from './config/environment.js';\n")
    parser = TypeScriptParser()
    ctx = ParseContext(path="index.ts", abs_path=tmp_path / "index.ts",
                       source=(tmp_path / "index.ts").read_bytes(), repo_root=tmp_path)
    rec = parser.parse_file(ctx)
    assert "config/environment.ts" in [p.replace("\\", "/") for p in rec.importFiles]
    assert "./config/environment.js" not in rec.externalImports


def test_alias_scoped_to_nearest_tsconfig(tmp_path) -> None:
    # Two apps each declare `@/*` -> their own `src`. A file in app-a importing `@/x` must
    # resolve to app-a's file, never app-b's same-named module (no cross-package leak).
    for app in ("app-a", "app-b"):
        (tmp_path / app).mkdir()
        (tmp_path / app / "tsconfig.json").write_text(
            '{ "compilerOptions": { "baseUrl": ".", "paths": { "@/*": ["./src/*"] } } }'
        )
        (tmp_path / app / "src").mkdir()
        (tmp_path / app / "src" / "x.ts").write_text(f"export const x = '{app}';\n")
    (tmp_path / "app-a" / "main.ts").write_text("import { x } from '@/x';\n")
    parser = TypeScriptParser()
    index = parser.build_index(tmp_path, [])
    ctx = ParseContext(path="app-a/main.ts", abs_path=tmp_path / "app-a" / "main.ts",
                       source=(tmp_path / "app-a" / "main.ts").read_bytes(),
                       repo_root=tmp_path, resolution_index=index)
    rec = parser.parse_file(ctx)
    resolved = [p.replace("\\", "/") for p in rec.importFiles]
    assert "app-a/src/x.ts" in resolved
    assert "app-b/src/x.ts" not in resolved


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


def test_build_index_skips_directory_path(tmp_path) -> None:
    """Fix 2: a path that is a directory (e.g. bulk-actions.service.ts/) must be silently
    skipped — not crash the index pre-pass with IsADirectoryError."""
    # Create a directory whose name looks like a .ts file (the real-repo bug pattern).
    dir_path = tmp_path / "bulk-actions.service.ts"
    dir_path.mkdir()
    (dir_path / "bulk-actions.service.ts").write_text("export class BulkActionsService {}\n")
    (tmp_path / "good.ts").write_text("export class GoodService {}\n")

    # Pass the directory path as if it were a file — must not raise.
    index = TypeScriptParser().build_index(
        tmp_path, [dir_path, tmp_path / "good.ts"], 1
    )
    # The good file is still indexed; the directory is silently skipped.
    assert isinstance(index, TsAliasIndex)
    assert "GoodService" in index.class_heritage


def test_build_index_survives_deeply_nested_file(tmp_path, capsys) -> None:
    """A pathologically deep AST (>recursion limit) must skip that one file, not abort the
    index pre-pass. Regression for the RecursionError in ``_collect_heritage.walk`` that
    crashed whole runs on a real repo (the pre-pass lacked the parse stage's isolation)."""
    depth = 2000  # comfortably past CPython's ~1000-frame default limit
    (tmp_path / "deep.ts").write_text(f"const x = {'[' * depth}{']' * depth};\n")
    (tmp_path / "good.ts").write_text("export class Foo extends Bar { m() {} }\n")

    # jobs=1 → serial, in-process: pre-fix this raised RecursionError right here.
    index = TypeScriptParser().build_index(tmp_path, [tmp_path / "deep.ts", tmp_path / "good.ts"], 1)

    assert isinstance(index, TsAliasIndex)
    assert "Foo" in index.class_heritage  # the good file was still indexed; only deep.ts skipped
    # the skip is logged with the offending file's name (so it is diagnosable, not silent)
    out = capsys.readouterr().out
    assert "index.file.skipped" in out and "deep.ts" in out
