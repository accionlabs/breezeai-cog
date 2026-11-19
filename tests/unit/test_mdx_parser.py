"""MDX language parser: a File record per ``.mdx`` file, with the imports of its ESM blocks."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from breezeai_cog import iter_file_records
from breezeai_cog.core.registry import discover_builtin, language_candidates
from breezeai_cog.parsers.mdx.esm import esm_lines
from breezeai_cog.parsers.mdx.parser import is_esm
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
    return is_esm(code)


def _esm(text: str) -> list[tuple[int, int]]:
    return esm_lines(text.encode(), _parses)


def test_mdx_file_is_a_code_file_of_language_mdx(capture) -> None:
    recs = capture({"docs/intro.mdx": "# Intro\n\nSome text.\n"})
    rec = recs["docs/intro.mdx"]
    assert (rec.type, rec.language, rec.loc) == ("code", "mdx", 0)
    assert rec.importFiles == [] and rec.externalImports == []
    assert [s.semanticType for s in rec.statements] == ["doc"]


def test_loc_counts_only_the_esm_lines(capture) -> None:
    """Prose, code samples and prose starting with ``import`` are not lines of code."""
    recs = capture(
        {
            "components/Chart.tsx": CHART,
            "docs/a.mdx": "import {\n  Chart,\n} from '../components/Chart'\n\n"
            "export const meta = {\n\n  title: 'A',\n}\n\n# A\n\nSome text.\n\n"
            "```js\nimport x from 'x'\n```\n\nimport the CSV and run it.\n",
        }
    )
    assert recs["docs/a.mdx"].loc == 6


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


@pytest.mark.parametrize("marker", ["- ", "* ", "1. ", "2) "])
def test_a_fence_opened_on_a_list_item_line_is_a_fence(marker: str) -> None:
    """A list item may open a fence on its marker line; its indented closing fence then closes
    it, so a later code sample's ``import`` is not read as ESM."""
    item = f"{marker}```bash\n   npm i foo\n   ```\n\n"
    sample = "```\nimport a from 'a'\n\nimport { Foo } from '../components/foo'\n\nrender(<Foo />)\n```\n"
    assert _esm("# Install\n\n" + item + "Then use it:\n\n" + sample) == []
    assert _esm(item + "import b from 'b'\n") == [(4, 5)]


def test_a_code_sample_after_a_list_item_fence_is_not_an_import(capture) -> None:
    recs = capture(
        {
            "components/foo.tsx": "export const Foo = () => null\n",
            "docs/guide.mdx": "# Install\n\n- ```bash\n  npm i foo\n  ```\n\nThen use it:\n\n"
            "```\nimport a from 'a'\n\nimport { Foo } from '../components/foo'\n\n"
            "render(<Foo />)\n```\n",
        }
    )
    rec = recs["docs/guide.mdx"]
    assert (rec.importFiles, rec.externalImports) == ([], [])


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


#: A lone quote in an expression's JSX text, then a code sample whose stray ``}`` would close
#: that expression mid-fence: the ``~~~`` lines after it would swap fence and text.
LOST_IN_EXPRESSION = (
    "{show && <p>Don't</p>}\n\n~~~css\n.a { }\n}\n~~~\n\n~~~\n\n"
    "import secret from './secret'\n\n~~~\n"
)


def test_a_lone_quote_in_an_expression_stops_reading_esm() -> None:
    """Once the scan loses track in an expression, no later line is ESM — not even after a
    stray ``}`` seems to close it."""
    assert _esm(LOST_IN_EXPRESSION) == []
    assert _esm("{show && <p>Don't</p>}\n\nText }\n\nimport a from 'a'\n") == []


def test_a_code_sample_after_a_lone_quote_gives_no_import(capture) -> None:
    recs = capture({"secret.ts": "export default 1\n", "docs/a.mdx": LOST_IN_EXPRESSION})
    assert (recs["docs/a.mdx"].importFiles, recs["docs/a.mdx"].externalImports) == ([], [])


def test_a_closed_string_in_an_expression_keeps_reading_esm() -> None:
    assert _esm("{show && <p>{'ok'}</p>}\n\nimport a from 'a'\n") == [(2, 3)]


class _Info:
    """Records the pipeline's ``info`` events; its other log calls are dropped."""

    def __init__(self) -> None:
        self.events: dict[str, dict] = {}

    def info(self, event: str, **kw) -> None:
        self.events[event] = kw

    def __getattr__(self, name: str) -> Callable[..., None]:
        return lambda *a, **kw: None


def test_mdx_documents_count_in_the_document_totals(tmp_path, monkeypatch) -> None:
    """An ``.mdx`` file is a Markdown document too: counted in ``markdown_docs``, while only
    its ESM lines count as LOC."""
    from breezeai_cog.config import Settings
    from breezeai_cog.core import pipeline
    from breezeai_cog.emit.sinks import MemorySink

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.md").write_text("# A\n\nText.\n")
    (repo / "b.mdx").write_text("import x from 'x'\n\n# B\n\nText.\n")
    log = _Info()
    monkeypatch.setattr(pipeline, "log", log)
    meta = pipeline.run(repo, Settings(jobs=1), MemorySink())
    assert log.events["analysis.complete"]["markdown_docs"] == 2
    assert meta.totalLinesOfCode == 1


def test_a_page_record_does_not_count_as_document_text(tmp_path, monkeypatch) -> None:
    """``markdown_chars`` counts an ``.mdx`` file's document statement, not the Next.js page
    record beside it."""
    from breezeai_cog.config import Settings
    from breezeai_cog.core import pipeline
    from breezeai_cog.emit.sinks import MemorySink

    repo = tmp_path / "repo"
    (repo / "app" / "about").mkdir(parents=True)
    (repo / "next.config.mjs").write_text(
        "const nextConfig = { pageExtensions: ['tsx', 'mdx'] };\nexport default nextConfig;\n"
    )
    page = "import x from 'x'\n\n# About\n\nText.\n"
    (repo / "app" / "about" / "page.mdx").write_text(page)
    log = _Info()
    monkeypatch.setattr(pipeline, "log", log)
    sink = MemorySink()
    pipeline.run(repo, Settings(jobs=1), sink)
    [rec] = [r for r in sink.records if r.path == "app/about/page.mdx"]
    assert sorted(st.semanticType or "" for st in rec.statements) == ["doc", "route"]
    assert log.events["analysis.complete"]["markdown_chars"] == len(page)


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


def test_a_block_that_never_parses_still_opens_its_fence() -> None:
    """The fence under a prose ``import`` line is still a fence: the code sample's import is
    not an import, and the closing fence does not open a new one."""
    text = (
        "import the CSV like this:\n```js\n// load\n\nimport { load } from './loader'\n\n"
        "load()\n```\n\nimport ok from 'ok'\n"
    )
    assert _esm(text) == [(9, 10)]


def test_a_fence_glued_under_an_import_is_still_a_fence() -> None:
    """``~~~js`` parses as an expression, but MDX compiles only import / export statements: the
    block is text, so its fence opens, the code sample's import is not ESM, and the closing
    fence does not hide the ESM after it."""
    text = "import a from 'a'\n~~~js\n\nimport evil from 'evil'\n\n~~~\n\nexport const later = 1\n"
    assert _esm(text) == [(7, 8)]


def test_a_code_sample_under_a_glued_fence_is_not_an_import(capture) -> None:
    recs = capture(
        {
            "components/Real.tsx": CHART,
            "components/Fake.tsx": CHART,
            "docs/a.mdx": "import { Real } from '../components/Real'\n~~~js\n\n"
            "import { Fake } from '../components/Fake'\n\n~~~\n\nexport const later = 1\n",
        }
    )
    rec = recs["docs/a.mdx"]
    assert (rec.importFiles, rec.exports) == ([], ["later"])


def test_jsx_glued_under_an_import_still_opens_its_element() -> None:
    assert _esm("import a from 'a'\n<Tabs>\n\nimport evil from 'evil'\n\n</Tabs>\n") == []


@pytest.mark.parametrize(
    "text",
    [
        "> import evil from 'evil'\n",  # blockquote
        "```js\n\nimport evil from 'evil'\n",  # fence left open to the end of the file
        "| a |\n|---|\n| import evil from 'evil' |\n",  # table row
        "import evil from 'evil'\n---\n",  # setext heading
    ],
)
def test_markdown_led_by_import_is_not_esm(text: str) -> None:
    assert _esm(text) == []


def test_a_shadow_that_still_does_not_parse_gives_no_imports(capture, monkeypatch) -> None:
    """When blanking the broken statements leaves a tree with errors, no import is read."""
    from breezeai_cog.parsers.mdx import parser as mdx_parser

    real = mdx_parser.blank_ranges
    calls: list[int] = []

    def blank_once_wrong(source: bytes, ranges: list[tuple[int, int]]) -> bytes:
        calls.append(1)
        return source if len(calls) == 1 else real(source, ranges)

    shadow = b"import a from 'a'\nimport b from\n"
    monkeypatch.setattr(mdx_parser, "esm_shadow", lambda *_a: shadow)
    monkeypatch.setattr(mdx_parser, "blank_ranges", blank_once_wrong)
    rec = capture({"docs/a.mdx": shadow.decode()})["docs/a.mdx"]
    assert (rec.importFiles, rec.externalImports, rec.loc) == ([], [], 0)


def test_parse_timeout_keeps_the_file_record(capture, monkeypatch) -> None:
    from breezeai_cog.parsers.mdx import parser as mdx_parser

    real = mdx_parser.parse_source

    def slow(grammar, code, timeout=0):
        if b"meta" in code and b"\n\n" in code:
            raise ValueError("Parsing failed")
        return real(grammar, code, timeout)

    monkeypatch.setattr(mdx_parser, "parse_source", slow)
    with capture_logs() as logs:
        recs = capture({"docs/a.mdx": "export const meta = {\n\n" + "word " * 20 + "\n"})
    assert recs["docs/a.mdx"].language == "mdx"
    # Several block probes time out; the file is reported once.
    [event] = [e for e in logs if e["event"] == "mdx.parse_timeout"]
    assert (event["path"], event["stage"]) == ("docs/a.mdx", "block")


def test_shadow_parse_timeout_keeps_the_file_record_without_imports(capture, monkeypatch) -> None:
    from breezeai_cog.parsers.mdx import parser as mdx_parser

    real = mdx_parser.parse_source
    source = "import { Chart } from '../components/Chart'\n\n# Sales\n"

    def slow(grammar, code, timeout=0):
        if len(code) == len(source):  # the whole-file shadow, not a block probe
            raise ValueError("Parsing failed")
        return real(grammar, code, timeout)

    monkeypatch.setattr(mdx_parser, "parse_source", slow)
    with capture_logs() as logs:
        recs = capture({"docs/a.mdx": source, "components/Chart.tsx": "export const Chart = 1\n"})
    rec = recs["docs/a.mdx"]
    assert (rec.language, rec.importFiles, rec.externalImports, rec.exports) == ("mdx", [], [], [])
    [event] = [e for e in logs if e["event"] == "mdx.parse_timeout"]
    assert (event["path"], event["stage"]) == ("docs/a.mdx", "file")
    # The document is still read; the import line, code the scan found, is not its outline.
    assert rec.metadata["title"] == "Sales"


def test_a_file_that_parses_logs_no_timeout(capture) -> None:
    with capture_logs() as logs:
        capture({"docs/a.mdx": "import Head from 'next/head'\n\n# A\n"})
    assert not [e for e in logs if e["event"] == "mdx.parse_timeout"]


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


def test_shadow_keeps_byte_offsets_with_bom_crlf_and_front_matter() -> None:
    """The shadow is the file's own length and an ESM line keeps its byte offset, so node
    positions in it are the file's lines and columns."""
    from breezeai_cog.parsers.mdx.esm import esm_shadow

    src = (
        "\ufeff---\r\ntitle: x\r\n---\r\n\r\nimport A from './a'\r\n\r\n# Hi\r\n\r\n"
        "```js\r\nimport B from './b'\r\n```\r\n\r\nexport const m = {\r\n\r\n  t: 1\r\n}\r\n"
    ).encode()
    shadow = esm_shadow(src, _parses)
    assert len(shadow) == len(src)
    assert shadow.count(b"\n") == src.count(b"\n")
    for code in (b"import A from './a'", b"export const m = {", b"  t: 1"):
        assert shadow.index(code) == src.index(code)
    assert b"import B" not in shadow and b"title" not in shadow and b"\xef" not in shadow


def test_parse_budget_reached_is_logged(capture) -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        capture({"docs/a.mdx": "import a\n\n" * 200})
    [event] = [e for e in logs if e["event"] == "mdx.esm_parse_budget_reached"]
    assert event["path"] == "docs/a.mdx"


def test_budget_spent_is_reported_only_when_esm_is_left_unread() -> None:
    """A file that uses exactly its whole budget and has no more ESM lost nothing: not
    reported. One more ESM block after it is left unread: reported once."""
    from breezeai_cog.parsers.mdx.esm import MAX_PARSES_PER_FILE

    reports: list[None] = []
    full = b"import a from 'a'\n\n" * MAX_PARSES_PER_FILE
    assert len(esm_lines(full, lambda _: True, lambda: reports.append(None))) == MAX_PARSES_PER_FILE
    assert reports == []
    esm_lines(
        full + b"import b from 'b'\n\nimport c from 'c'\n",
        lambda _: True,
        lambda: reports.append(None),
    )
    assert reports == [None]


def test_parse_budget_not_reached_is_not_logged(capture) -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        capture({"docs/a.mdx": "import Head from 'next/head'\n\n# A\n"})
    assert not [e for e in logs if e["event"] == "mdx.esm_parse_budget_reached"]


def test_a_paragraph_over_the_cap_is_not_parsed_as_continuation() -> None:
    """A paragraph that would take a continued block past the cap ends the search: only the
    block's own paragraph is parsed."""
    from breezeai_cog.parsers.mdx.esm import MAX_CONTINUATION_BYTES

    calls: list[int] = []

    def parses(code: bytes) -> bool:
        calls.append(len(code))
        return False

    big = ("word " * (MAX_CONTINUATION_BYTES // 4) + "\n").encode()
    esm_lines(b"export const meta = {\n\n" + big, parses)
    assert calls == [len(b"export const meta = {")]


def test_upper_case_mdx_suffix(capture) -> None:
    recs = capture(
        {"docs/Guide.MDX": "import { Chart } from './Chart'\n\n# Guide\n", "docs/Chart.tsx": CHART}
    )
    rec = recs["docs/Guide.MDX"]
    assert (rec.language, rec.importFiles) == ("mdx", ["docs/Chart.tsx"])


def test_the_prose_is_captured_as_a_markdown_document(capture) -> None:
    """An MDX file is read as the Markdown parser reads a document: front matter, title and
    headings in ``metadata``, and the whole file as one ``doc`` statement — and it is still a
    code file with its imports."""
    src = (
        "---\ntitle: Button\ndescription: The button component\n---\n\n"
        "import { Meta } from '@storybook/addon-docs/blocks'\n\n"
        '# Button\n\n## Usage\n\n<Meta title="Button" />\n'
    )
    rec = capture({"libs/button.mdx": src})["libs/button.mdx"]
    assert (rec.type, rec.language, rec.externalImports) == (
        "code",
        "mdx",
        ["@storybook/addon-docs/blocks"],
    )
    assert rec.metadata["kind"] == "doc" and rec.metadata["category"] == "documentation"
    assert rec.metadata["frontmatter"] == {"title": "Button", "description": "The button component"}
    assert rec.metadata["title"] == "Button"
    assert [(h["level"], h["text"], h["line"]) for h in rec.metadata["headings"]] == [
        (1, "Button", 8),
        (2, "Usage", 10),
    ]
    [doc] = rec.statements
    assert (doc.semanticType, doc.nodeType, doc.id, doc.parentId) == (
        "doc",
        "synthetic",
        "libs/button.mdx:1:0",
        "libs/button.mdx",
    )
    assert (doc.name, doc.description, doc.startLine, doc.endLine) == (
        "Button",
        "The button component",
        1,
        12,
    )
    assert doc.text == src


def test_a_hash_line_inside_an_esm_block_is_not_a_heading(capture) -> None:
    src = "export const md = `\n# Not a heading\n`\n\n# Real\n"
    rec = capture({"docs/a.mdx": src})["docs/a.mdx"]
    assert [h["text"] for h in rec.metadata["headings"]] == ["Real"]
    assert rec.metadata["title"] == "Real"


def test_a_hash_line_in_a_code_sample_is_not_a_heading(capture) -> None:
    rec = capture({"docs/a.mdx": "```sh\n# install\n```\n\n# Guide\n"})["docs/a.mdx"]
    assert [h["text"] for h in rec.metadata["headings"]] == ["Guide"]


@pytest.mark.parametrize(
    ("path", "kind"),
    [("README.mdx", "readme"), ("CHANGELOG.mdx", "changelog"), ("docs/guide.mdx", "doc")],
)
def test_the_document_kind_follows_the_markdown_rules(capture, path: str, kind: str) -> None:
    assert capture({path: "# Title\n"})[path].metadata["kind"] == kind


def test_secrets_in_the_document_are_masked(capture) -> None:
    src = "# Setup\n\n```sh\nexport DB_PASSWORD=hunter2\n```\n"
    [doc] = capture({"docs/a.mdx": src})["docs/a.mdx"].statements
    assert "hunter2" not in doc.text and "DB_PASSWORD=***" in doc.text


def test_document_metadata_without_statement_capture(capture) -> None:
    rec = capture({"docs/a.mdx": "# Intro\n"}, capture_statements=False)["docs/a.mdx"]
    assert rec.metadata["title"] == "Intro" and rec.statements == []


def test_the_mdx_document_matches_the_markdown_one(capture) -> None:
    """Pure Markdown in an ``.mdx`` file gives the same document as the same text in ``.md``."""
    src = "---\ntitle: T\n---\n\n# T\n\n## Part\n\nText with `code`.\n"
    recs = capture({"docs/a.mdx": src, "docs/a.md": src})
    mdx, md = recs["docs/a.mdx"], recs["docs/a.md"]
    assert mdx.metadata == md.metadata
    [mdx_doc], [md_doc] = mdx.statements, md.statements
    assert (mdx_doc.semanticType, mdx_doc.text, mdx_doc.name) == (
        md_doc.semanticType,
        md_doc.text,
        md_doc.name,
    )


def test_lone_carriage_returns_keep_the_outline_on_its_lines(capture) -> None:
    """A lone ``\\r`` ends a document line but not a scanner line: the ESM line is mapped into
    the document's numbering, so the heading above it is kept."""
    src = "a\rb\rc\n# Intro\n\nimport A from './a'\n\n## Next\n"
    rec = capture({"docs/a.mdx": src})["docs/a.mdx"]
    assert [(h["text"], h["line"]) for h in rec.metadata["headings"]] == [("Intro", 4), ("Next", 8)]


def test_a_block_timeout_stops_probing_and_leaves_no_outline(capture, monkeypatch) -> None:
    """After one block probe times out the file is not probed again, and since which lines are
    code is then unknown, no heading is guessed — a ``#`` in an export is never one."""
    from breezeai_cog.parsers.mdx import parser as mdx_parser

    real = mdx_parser.parse_source
    probes: list[bytes] = []

    def slow(grammar, code, timeout=0):
        if b"md" in code and len(code) < 200:  # a block probe of the export
            probes.append(code)
            raise ValueError("Parsing failed")
        return real(grammar, code, timeout)

    monkeypatch.setattr(mdx_parser, "parse_source", slow)
    src = (
        "export const md = `\n# Not a heading\n`\n\n# Real\n\n"
        "export const md2 = 1\n\nimport A from 'a'\n"
    )
    with capture_logs() as logs:
        rec = capture({"docs/a.mdx": src})["docs/a.mdx"]
    assert len(probes) == 1
    assert rec.metadata["headings"] == [] and rec.metadata["title"] is None
    assert rec.externalImports == []
    assert [e["stage"] for e in logs if e["event"] == "mdx.parse_timeout"] == ["block"]


def test_block_and_file_timeouts_are_both_logged(capture, monkeypatch) -> None:
    from breezeai_cog.parsers.mdx import parser as mdx_parser

    def slow(grammar, code, timeout=0):
        raise ValueError("Parsing failed")

    real = mdx_parser.parse_source
    monkeypatch.setattr(
        mdx_parser, "parse_source", lambda g, c, t=0: slow(g, c, t) if c else real(g, c, t)
    )
    with capture_logs() as logs:
        rec = capture({"docs/a.mdx": "import A from 'a'\n\n# T\n"})["docs/a.mdx"]
    assert rec.language == "mdx"
    stages = [e["stage"] for e in logs if e["event"] == "mdx.parse_timeout"]
    assert stages == ["block", "file"]
