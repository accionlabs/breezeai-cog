"""Vite ``resolve.alias`` read into import-resolution scopes (parsers/typescript/vite_aliases.py)."""

from __future__ import annotations

import pickle
from pathlib import Path

import pytest

from breezeai_cog.parsers.typescript.imports import _resolve, build_alias_index

URL_FORM = "fileURLToPath(new URL('./src', import.meta.url))"
RESOLVE_FORM = "path.resolve(__dirname, './src')"


def _repo(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def _config(alias: str, *, export: str = "export default defineConfig({0});") -> str:
    return export.format("{ resolve: { alias: " + alias + " } }")


def _resolved(root: Path, module: str, from_file: str = "src/main.ts") -> str | None:
    return _resolve(module, from_file, root, build_alias_index(root))


def _with_src(root: Path, config: str, name: str = "vite.config.js") -> Path:
    return _repo(
        root, {name: config, "src/views/Home.vue": "", "src/index.ts": "", "src/main.ts": ""}
    )


@pytest.mark.parametrize(
    "target",
    [
        URL_FORM,
        RESOLVE_FORM,
        "path.resolve(__dirname, 'src')",
        "resolve(__dirname, './src')",
        "resolve(import.meta.dirname, 'src')",
        "path.resolve(__dirname, '.', 'src')",
    ],
)
def test_object_form_resolves_the_forms_read(tmp_path, target) -> None:
    _with_src(tmp_path, _config("{ '@': " + target + " }"))
    assert _resolved(tmp_path, "@/views/Home") == "src/views/Home.vue"


def test_scope_crosses_the_process_boundary(tmp_path) -> None:
    _with_src(tmp_path, _config("{ '@': " + URL_FORM + " }"))
    index = build_alias_index(tmp_path)
    assert index is not None
    assert pickle.loads(pickle.dumps(index)).alias_scopes == index.alias_scopes
    cfg_dir, patterns = index.alias_scopes[0]
    assert cfg_dir == str(tmp_path.resolve())
    assert patterns == {"@": [str(tmp_path / "src")], "@/*": [str(tmp_path / "src") + "/*"]}


def test_array_form_skips_a_regexp_find(tmp_path) -> None:
    alias = (
        "[{ find: /^~\\//, replacement: 'x' }, { find: '@', replacement: " + RESOLVE_FORM + " }]"
    )
    _with_src(tmp_path, _config(alias))
    assert _resolved(tmp_path, "@/views/Home") == "src/views/Home.vue"
    assert _resolved(tmp_path, "~/views/Home") is None


@pytest.mark.parametrize(
    "config",
    [
        "export default { resolve: { alias: { '@': " + URL_FORM + " } } };",
        "export default defineConfig({ resolve: { alias: { '@': " + URL_FORM + " } } });",
        "module.exports = { resolve: { alias: { '@': " + RESOLVE_FORM + " } } };",
        "module.exports = defineConfig({ resolve: { alias: { '@': " + RESOLVE_FORM + " } } });",
        "export default { resolve: { alias: { '@': " + URL_FORM + " } } } satisfies UserConfig;",
        "export default ({ resolve: { alias: { '@': " + URL_FORM + " } } });",
        "export default defineConfig({ resolve: { alias: { '@': "
        + URL_FORM
        + " } } } as UserConfig);",
    ],
)
def test_config_shapes_read(tmp_path, config) -> None:
    _with_src(tmp_path, config)
    assert _resolved(tmp_path, "@/views/Home") == "src/views/Home.vue"


@pytest.mark.parametrize(
    "config",
    [
        # function form and a config bound to a variable are not read
        "export default defineConfig(() => ({ resolve: { alias: { '@': " + URL_FORM + " } } }));",
        "const config = { resolve: { alias: { '@': " + URL_FORM + " } } };\nexport default config;",
        "export default defineConfig({ resolve: { alias: aliases } });",
    ],
)
def test_config_shapes_not_read_are_left_alone(tmp_path, config) -> None:
    _with_src(tmp_path, config)
    assert build_alias_index(tmp_path) is None
    assert _resolved(tmp_path, "@/views/Home") is None


@pytest.mark.parametrize(
    "target",
    [
        "srcDir",  # a variable
        "`${root}/src`",  # a template
        "'./src'",  # relative string: Vite uses it as-is
        "'/src'",  # absolute string
        "path.join(__dirname, 'src')",  # another call
        "path.resolve(process.cwd(), 'src')",  # not the config's own folder
        "path.resolve(__dirname, srcName)",  # a non-literal segment
        "path.resolve(__dirname, '/abs')",  # an absolute segment restarts the path
        "fileURLToPath(new URL('./src', base))",  # not import.meta.url
        "fileURLToPath(new URL('/src', import.meta.url))",
    ],
)
def test_replacements_that_are_not_literal_are_left_alone(tmp_path, target) -> None:
    _with_src(tmp_path, _config("{ '@': " + target + " }"))
    assert build_alias_index(tmp_path) is None
    assert _resolved(tmp_path, "@/views/Home") is None


def test_key_matches_itself_or_a_slash_prefix_only(tmp_path) -> None:
    _with_src(tmp_path, _config("{ '@': " + URL_FORM + " }"))
    assert _resolved(tmp_path, "@") == "src/index.ts"
    assert _resolved(tmp_path, "@/views/Home") == "src/views/Home.vue"
    assert _resolved(tmp_path, "@vue/runtime-core") is None
    assert _resolved(tmp_path, "@scope/pkg") is None


@pytest.mark.parametrize("find", ["''", "'@/'", "'@*'"])
def test_keys_that_are_not_plain_names_are_skipped(tmp_path, find) -> None:
    _with_src(tmp_path, _config("{ " + find + ": " + URL_FORM + " }"))
    assert build_alias_index(tmp_path) is None


def test_unquoted_key_is_a_name(tmp_path) -> None:
    _with_src(tmp_path, _config("{ views: path.resolve(__dirname, 'src/views') }"))
    assert _resolved(tmp_path, "views/Home") == "src/views/Home.vue"


def test_target_outside_the_repo_is_dropped(tmp_path) -> None:
    repo = _repo(
        tmp_path / "repo",
        {
            "vite.config.ts": _config("{ '@shared': path.resolve(__dirname, '../shared') }"),
            "src/main.ts": "",
        },
    )
    _repo(tmp_path, {"shared/util.ts": ""})
    assert build_alias_index(repo) is None
    assert _resolved(repo, "@shared/util") is None


def test_sibling_configs_stay_apart(tmp_path) -> None:
    # Two apps each alias `@` to their own `src`; app-a must never reach app-b's module.
    for app in ("app-a", "app-b"):
        _repo(
            tmp_path,
            {
                f"{app}/vite.config.ts": _config("{ '@': " + URL_FORM + " }"),
                f"{app}/src/x.ts": "",
            },
        )
    _repo(tmp_path, {"app-a/main.ts": ""})
    assert _resolved(tmp_path, "@/x", "app-a/main.ts") == "app-a/src/x.ts"


def test_target_inside_node_modules_is_dropped(tmp_path) -> None:
    # Dependency files are never captured: an alias to one would give an edge to no node.
    _repo(
        tmp_path,
        {
            "vite.config.ts": _config(
                "{ vue: path.resolve(__dirname, 'node_modules/vue/dist/vue.js') }"
            ),
            "node_modules/vue/dist/vue.js": "",
            "main.ts": "",
        },
    )
    assert build_alias_index(tmp_path) is None
    assert _resolved(tmp_path, "vue", "main.ts") is None


def test_the_nearest_enclosing_config_wins(tmp_path) -> None:
    # A root config and an app's own config both enclose the app's files; the app's `@` wins,
    # the root's still serves files outside the app.
    _repo(
        tmp_path,
        {
            "vite.config.ts": _config("{ '@': " + URL_FORM + " }"),
            "src/x.ts": "",
            "src/main.ts": "",
            "app/vite.config.ts": _config("{ '@': " + URL_FORM + " }"),
            "app/src/x.ts": "",
            "app/main.ts": "",
        },
    )
    assert _resolved(tmp_path, "@/x", "app/main.ts") == "app/src/x.ts"
    assert _resolved(tmp_path, "@/x", "src/main.ts") == "src/x.ts"


def test_configs_in_one_folder_are_read_in_name_order(tmp_path, monkeypatch) -> None:
    # The first config's target wins, so the order must not depend on the file system's listing.
    from breezeai_cog.parsers.typescript import imports

    real_walk = imports.os.walk

    def listed_backwards(top):
        for dirpath, dirnames, filenames in real_walk(top):
            yield dirpath, dirnames, sorted(filenames, reverse=True)

    monkeypatch.setattr(imports.os, "walk", listed_backwards)
    _repo(
        tmp_path,
        {
            "vite.config.js": _config("{ '@': path.resolve(__dirname, 'a') }"),
            "vite.config.ts": _config("{ '@': path.resolve(__dirname, 'b') }"),
            "a/x.ts": "",
            "b/x.ts": "",
            "main.ts": "",
        },
    )
    assert _resolved(tmp_path, "@/x", "main.ts") == "a/x.ts"


def test_tsconfig_and_vite_config_in_one_folder_both_apply(tmp_path) -> None:
    _repo(
        tmp_path,
        {
            "tsconfig.json": '{ "compilerOptions": { "baseUrl": ".", "paths": { "@/*": ["ts-src/*"] } } }',
            "vite.config.ts": _config("{ '@': " + URL_FORM + ", '~': " + URL_FORM + " }"),
            "ts-src/x.ts": "",
            "src/x.ts": "",
            "src/y.ts": "",
            "src/main.ts": "",
        },
    )
    assert _resolved(tmp_path, "@/x") == "ts-src/x.ts"  # tsconfig is tried first
    assert _resolved(tmp_path, "@/y") == "src/y.ts"  # the Vite alias covers what it lacks
    assert _resolved(tmp_path, "~/x") == "src/x.ts"


@pytest.mark.parametrize(
    "name",
    [
        "vite.config.ts",
        "vite.config.mts",
        "vite.config.mjs",
        "vite.config.cjs",
        "vite.config.dev.ts",
    ],
)
def test_config_file_names_read(tmp_path, name) -> None:
    _with_src(tmp_path, _config("{ '@': " + URL_FORM + " }"), name)
    assert _resolved(tmp_path, "@/views/Home") == "src/views/Home.vue"


def test_other_bundler_configs_are_not_read(tmp_path) -> None:
    _with_src(tmp_path, _config("{ '@': " + URL_FORM + " }"), "vitest.config.ts")
    assert build_alias_index(tmp_path) is None


def test_config_in_dependencies_and_build_output_is_pruned(tmp_path) -> None:
    config = _config("{ '@': " + URL_FORM + " }")
    _repo(tmp_path, {f"{d}/vite.config.js": config for d in ("node_modules/pkg", "dist")})
    assert build_alias_index(tmp_path) is None


def test_config_without_alias_or_with_a_syntax_error_gives_nothing(tmp_path) -> None:
    _repo(
        tmp_path,
        {
            "a/vite.config.js": "export default defineConfig({ plugins: [] });",
            "b/vite.config.js": "export default {{{ resolve alias ((",
        },
    )
    assert build_alias_index(tmp_path) is None


def test_unreadable_config_is_skipped_with_a_warning(tmp_path, monkeypatch) -> None:
    # A dangling symlink named like a config: reading fails, indexing goes on, the skip is logged.
    # The logger is replaced by a recorder, so the test does not depend on how logging is configured.
    from breezeai_cog.parsers.typescript import vite_aliases

    warnings: list[tuple[str, dict[str, object]]] = []

    class Recorder:
        def warning(self, event: str, **fields: object) -> None:
            warnings.append((event, fields))

    monkeypatch.setattr(vite_aliases, "get_logger", lambda name: Recorder())
    (tmp_path / "vite.config.ts").symlink_to(tmp_path / "missing.ts")
    _with_src(tmp_path / "app", _config("{ '@': " + URL_FORM + " }"))
    index = build_alias_index(tmp_path)
    assert index is not None
    assert [d for d, _ in index.alias_scopes] == [str((tmp_path / "app").resolve())]
    [(event, fields)] = warnings
    assert event == "vite.alias.skipped"
    assert fields["error_type"] == "FileNotFoundError"
    assert str(fields["path"]).endswith("vite.config.ts")
