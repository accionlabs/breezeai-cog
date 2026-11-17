"""MDX language parser: a File record per ``.mdx`` file, with the imports of its ESM blocks."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from breezeai_cog import iter_file_records
from breezeai_cog.core.registry import discover_builtin, language_candidates
from breezeai_cog.parsers.mdx.esm import esm_lines
from breezeai_cog.parsers.treesitter import parse_source
from breezeai_cog.schemas import FileRecord

CHART = "export function Chart() {\n  return null;\n}\n"


@pytest.fixture
def capture(tmp_path: Path) -> Callable[..., dict[str, FileRecord]]:
    """Write ``{path: source}`` into a repo and return path → File record."""

    def run(files: dict[str, str], **kwargs: object) -> dict[str, FileRecord]:
        repo = tmp_path / "repo"
        for rel, src in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(src.encode())
        return {r.path: r for r in iter_file_records(repo, **kwargs)}

    return run


def _parses(code: bytes) -> bool:
    return not parse_source("tsx", code, 0).root_node.has_error


def _esm(text: str) -> list[tuple[int, int]]:
    return esm_lines(text.encode(), _parses)


def test_mdx_file_is_a_code_file_of_language_mdx(capture) -> None:
    recs = capture({"docs/intro.mdx": "# Intro\n\nSome text.\n"})
    rec = recs["docs/intro.mdx"]
    assert (rec.type, rec.language, rec.loc) == ("code", "mdx", 2)
    assert rec.importFiles == [] and rec.externalImports == [] and rec.statements == []


def test_only_the_mdx_parser_registers_mdx() -> None:
    """Another language claiming ``.mdx`` (a Markdown parser) would make it a shared extension
    and take MDX files away from this parser."""
    discover_builtin()
    assert [p.name for p in language_candidates("app/about/page.mdx")] == ["mdx"]


def test_relative_and_external_imports(capture) -> None:
    recs = capture(
        {
            "components/Chart.tsx": CHART,
            "docs/report.mdx": "import { Chart } from '../components/Chart'\n"
            "import Head from 'next/head'\n\n# Sales report\n\n<Chart values={[1, 2, 3]} />\n",
        }
    )
    rec = recs["docs/report.mdx"]
    assert rec.importFiles == ["components/Chart.tsx"]
    assert rec.externalImports == ["next/head"]


def test_tsconfig_alias_resolves(capture) -> None:
    recs = capture(
        {
            "tsconfig.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"]}}}',
            "src/components/Chart.tsx": CHART,
            "src/app/page.tsx": "export default function Home() { return null; }\n",
            "content/post.mdx": "import { Chart } from '@/components/Chart'\n\n<Chart />\n",
        }
    )
    assert recs["content/post.mdx"].importFiles == ["src/components/Chart.tsx"]


def test_multi_line_import_and_exports(capture) -> None:
    recs = capture(
        {
            "components/Chart.tsx": CHART,
            "docs/a.mdx": "import {\n  Chart,\n} from '../components/Chart'\n"
            "export const meta = { title: 'A' }\n\n# A\n",
        }
    )
    rec = recs["docs/a.mdx"]
    assert rec.importFiles == ["components/Chart.tsx"]
    assert rec.exports == ["meta"]


def test_export_continues_over_a_blank_line(capture) -> None:
    """MDX keeps an ESM block going while its code does not parse yet."""
    recs = capture(
        {
            "docs/a.mdx": "export const meta = {\n\n  title: 'A',\n}\n\n# A\n",
        }
    )
    assert recs["docs/a.mdx"].exports == ["meta"]
    assert _esm("export const meta = {\n\n  title: 'A',\n}\n\n# A\n") == [(0, 4)]


def test_cut_off_export_is_not_captured(capture) -> None:
    recs = capture({"docs/a.mdx": "export const meta = {\n\n# A\n\nText.\n"})
    assert recs["docs/a.mdx"].exports == []


def test_prose_starting_with_import_or_export_is_not_code(capture) -> None:
    recs = capture(
        {
            "docs/a.mdx": "# Data\n\nimport the data from the API first.\n\n"
            "export default is the usual choice.\n",
        }
    )
    rec = recs["docs/a.mdx"]
    assert (rec.importFiles, rec.externalImports, rec.exports) == ([], [], [])


def test_import_inside_a_fenced_code_block_is_not_an_import(capture) -> None:
    recs = capture(
        {
            "components/Chart.tsx": CHART,
            "docs/a.mdx": "# Usage\n\n```js\nimport { Chart } from '../components/Chart'\n```\n\n"
            "~~~\n\nimport x from 'tilde-fence'\n~~~\n",
        }
    )
    rec = recs["docs/a.mdx"]
    assert (rec.importFiles, rec.externalImports) == ([], [])


def test_nested_fences_follow_commonmark() -> None:
    """A ```` fence is closed only by ```` or longer; the inner ``` lines are its content."""
    text = "````md\n```js\n\nimport a from 'a'\n```\n````\n\nimport b from 'b'\n"
    assert _esm(text) == [(7, 8)]


def test_an_indented_fence_is_a_fence() -> None:
    """MDX has no indented code, so a fence indented 4+ spaces (or a tab) still opens and
    closes a code block; the column-0 ``import`` inside it is a code sample."""
    text = "Install:\n\n    ```js\n    npm i\n\nimport { X } from '../internal/x'\n\n    ```\n\n"
    assert _esm(text + "import b from 'b'\n") == [(9, 10)]
    assert _esm("\t~~~\n\nimport a from 'a'\n\n\t~~~\n") == []


def test_import_inside_a_jsx_expression_is_not_an_import() -> None:
    text = (
        "<Code code={`\n\nimport { Button } from '@/components/button'\n\n`} />\n\n"
        "import real from 'real'\n"
    )
    assert _esm(text) == [(6, 7)]


def test_import_inside_a_jsx_element_is_not_an_import() -> None:
    text = "<Tabs>\n\nimport a from 'a'\n\n</Tabs>\n\nimport b from 'b'\n"
    assert _esm(text) == [(6, 7)]


def test_import_continuing_a_paragraph_is_not_an_import() -> None:
    assert _esm("Some text\nimport a from 'a'\n") == []


def test_indented_import_is_not_an_import() -> None:
    assert _esm("  import a from 'a'\n") == []


def test_inline_code_with_a_brace_does_not_open_an_expression() -> None:
    assert _esm("Use `{` here.\n\nimport a from 'a'\n") == [(2, 3)]


def test_front_matter_is_skipped(capture) -> None:
    recs = capture(
        {
            "docs/a.mdx": "---\ntitle: import x from 'y'\n---\nimport Head from 'next/head'\n\n# A\n",
        }
    )
    assert recs["docs/a.mdx"].externalImports == ["next/head"]


def test_bom_and_crlf(capture) -> None:
    recs = capture({"docs/a.mdx": "﻿import Head from 'next/head'\r\n\r\n# A\r\n"})
    rec = recs["docs/a.mdx"]
    assert rec.externalImports == ["next/head"]


def test_empty_file(capture) -> None:
    rec = capture({"docs/empty.mdx": ""})["docs/empty.mdx"]
    assert (rec.language, rec.loc, rec.importFiles) == ("mdx", 0, [])


def test_imports_are_captured_without_statements(capture) -> None:
    recs = capture({"docs/a.mdx": "import Head from 'next/head'\n"}, capture_statements=False)
    assert recs["docs/a.mdx"].externalImports == ["next/head"]


def test_fence_closes_only_on_a_bare_run() -> None:
    """``` ``` ``` inside a fence is content, not its closing line."""
    assert _esm("```md\n``` ```\n\nimport evil from 'evil'\n\n```\n") == []


def test_import_inside_an_html_comment_is_not_an_import() -> None:
    text = "# Doc\n\n<!--\n\nimport Old from '../components/Old'\n\n-->\n\nimport ok from 'ok'\n"
    assert _esm(text) == [(8, 9)]


@pytest.mark.parametrize("name", ["_Wrap", "$Wrap", "Ärger"])
def test_jsx_element_names_beyond_ascii_letters(name: str) -> None:
    assert _esm(f"<{name}>\n\nimport evil from 'evil'\n\n</{name}>\n") == []


def test_keyword_needs_a_space() -> None:
    assert _esm("import\tok from 'ok'\n\nimport{ a } from 'a'\n") == []


def test_a_block_that_never_parses_does_not_hide_the_next_import() -> None:
    assert _esm("export const a = [\n\n# Heading\n\n]x\n\nimport ok from 'ok'\n") == [(6, 7)]


def test_parse_timeout_keeps_the_file_record(capture, monkeypatch) -> None:
    from breezeai_cog.parsers.mdx import parser as mdx_parser

    real = mdx_parser.parse_source

    def slow(grammar, code, timeout=0):
        if b"meta" in code and b"\n\n" in code:
            raise ValueError("Parsing failed")
        return real(grammar, code, timeout)

    monkeypatch.setattr(mdx_parser, "parse_source", slow)
    recs = capture({"docs/a.mdx": "export const meta = {\n\n" + "word " * 20 + "\n"})
    assert recs["docs/a.mdx"].language == "mdx"


def test_shadow_parse_timeout_keeps_the_file_record_without_imports(capture, monkeypatch) -> None:
    from breezeai_cog.parsers.mdx import parser as mdx_parser

    real = mdx_parser.parse_source
    source = "import { Chart } from '../components/Chart'\n\n# Sales\n"

    def slow(grammar, code, timeout=0):
        if len(code) == len(source):  # the whole-file shadow, not a block probe
            raise ValueError("Parsing failed")
        return real(grammar, code, timeout)

    monkeypatch.setattr(mdx_parser, "parse_source", slow)
    recs = capture({"docs/a.mdx": source, "components/Chart.tsx": "export const Chart = 1\n"})
    rec = recs["docs/a.mdx"]
    assert (rec.language, rec.importFiles, rec.externalImports, rec.exports) == ("mdx", [], [], [])


def test_continuation_is_bounded_by_size() -> None:
    from breezeai_cog.parsers.mdx.esm import MAX_CONTINUATION_BYTES

    calls: list[int] = []

    def parses(code: bytes) -> bool:
        calls.append(len(code))
        return False

    para = ("word " * 200 + "\n\n").encode()
    esm_lines(b"export const meta = {\n\n" + para * 100, parses)
    assert max(calls) <= MAX_CONTINUATION_BYTES


def test_block_parses_are_bounded_per_file() -> None:
    """A file of thousands of ``import``-led paragraphs that never parse costs at most the
    file's budget, not ``1 + MAX_CONTINUATION_PARAGRAPHS`` parses per paragraph."""
    from breezeai_cog.parsers.mdx.esm import MAX_PARSES_PER_FILE

    calls = 0

    def parses(code: bytes) -> bool:
        nonlocal calls
        calls += 1
        return False

    assert esm_lines(b"import a\n\n" * 20_000, parses) == []
    assert calls == MAX_PARSES_PER_FILE


def test_upper_case_mdx_suffix(capture) -> None:
    recs = capture(
        {"docs/Guide.MDX": "import { Chart } from './Chart'\n\n# Guide\n", "docs/Chart.tsx": CHART}
    )
    rec = recs["docs/Guide.MDX"]
    assert (rec.language, rec.importFiles) == ("mdx", ["docs/Chart.tsx"])
