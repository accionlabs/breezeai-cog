"""MarkdownParser (BREEZEAI-1457): one File + one whole-file ``document`` statement per
Markdown file, classified by name / location / front matter; defensive front matter; secrets
masked; the 64 KB cut and the 512 KB File-only limit; pipeline totals and the doc-count cap."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog.core import pipeline, registry
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.markdown.parser import (
    MAX_STATEMENT_FILE_BYTES,
    MAX_TEXT_BYTES,
    MarkdownParser,
)
from breezeai_cog.schemas import ProjectMetaData


def _parse(path: str, text: str | bytes, *, repo_root: Path | str = ".", capture: bool = True):
    src = text.encode() if isinstance(text, str) else text
    ctx = ParseContext(
        path=path, abs_path=None, source=src, repo_root=repo_root, capture_statements=capture
    )
    return MarkdownParser().parse_file(ctx)


def _kind(path: str, text: str = "# Title\n", **kw) -> str:
    rec = _parse(path, text, **kw)
    assert rec.metadata["kind"] == rec.statements[0].semanticType
    return rec.metadata["kind"]


SKILL = """---
name: search
description: >
  Search the functional or code graph
  to answer questions.
---
# Search skill

Use it.
"""


# ── record shape ─────────────────────────────────────────────────────────────


def test_skill_record_and_statement_shape() -> None:
    rec = _parse("skills/search/SKILL.md", SKILL)
    assert (rec.type, rec.language, rec.id) == ("config", "markdown", "skills/search/SKILL.md")
    md = rec.metadata
    assert md["kind"] == "skill" and md["category"] == "documentation"
    assert md["frontmatter"]["name"] == "search"
    assert md["title"] == "Search skill"
    assert md["headings"] == [{"level": 1, "text": "Search skill", "line": 7}]
    [st] = rec.statements
    assert st.nodeType == "document" and st.semanticType == "skill"
    assert st.id == "skills/search/SKILL.md:1:0" and st.parentId == rec.id
    assert (st.startLine, st.endLine) == (1, 9)
    assert st.text == SKILL
    assert st.name == "search"
    # folded `>` string: one line, trailing newline stripped
    assert st.description == "Search the functional or code graph to answer questions."


def test_no_statement_without_statement_capture() -> None:
    rec = _parse("skills/search/SKILL.md", SKILL, capture=False)
    assert rec.statements == [] and rec.metadata["kind"] == "skill"


def test_markdown_owns_md_mdx_mdc_and_readme_rst() -> None:
    registry.discover_builtin()
    for path in (
        "a/notes.md",
        "page.mdx",
        ".cursor/rules/x.mdc",
        "README.md",
        "README.rst",
        ".cursorrules",
    ):
        assert registry.select(path, b"").name == "markdown", path


# ── classification ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "text", "kind"),
    [
        ("skills/search/SKILL.md", SKILL, "skill"),
        (".claude/skills/x/SKILL.md", SKILL, "skill"),
        ("skills/search/SKILL.md", "# no front matter\n", "doc"),
        ("agents/reviewer.md", "---\nname: reviewer\ndescription: Reviews code\n---\n", "agent"),
        (".claude/agents/r.md", "---\nname: r\ndescription: d\n---\n", "agent"),
        ("agents/notes.md", "# notes without front matter\n", "doc"),
        (".github/agents/planner.agent.md", "# planner\n", "agent"),
        (".claude/commands/deploy.md", "Deploy the app\n", "command"),
        ("plugin/commands/x.md", "---\ndescription: does x\n---\n", "command"),
        ("docs/commands/build.md", "# build CLI docs\n", "doc"),
        (".github/prompts/review.prompt.md", "Review this\n", "command"),
        ("skills/x/references/agent.prompt.md", "A template\n", "doc"),
        ("CLAUDE.md", "# rules\n", "agent_instructions"),
        ("pkg/AGENTS.md", "# rules\n", "agent_instructions"),
        ("GEMINI.md", "# rules\n", "agent_instructions"),
        (".github/copilot-instructions.md", "rules\n", "agent_instructions"),
        (".cursorrules", "rules\n", "agent_instructions"),
        (
            ".cursor/rules/style.mdc",
            "---\ndescription: style\n---\nUse tabs\n",
            "agent_instructions",
        ),
        ("README.md", "# Project\n", "readme"),
        ("sub/readme.md", "# Sub\n", "readme"),
        ("README.rst", "Project\n=======\n", "readme"),
        ("CHANGELOG.md", "# Changelog\n", "changelog"),
        ("HISTORY.md", "# History\n", "changelog"),
        ("docs/adr/0001-use-neo4j.md", "# ADR\n", "adr"),
        ("docs/decisions/x.md", "# x\n", "adr"),
        ("arch/0007-split.md", "# Split\n## Status\nAccepted\n## Decision\nSplit it\n", "adr"),
        ("arch/0007-split.md", "# Split\n## Notes\n", "doc"),
        ("docs/setup.md", "# Setup\n", "doc"),
        ("page.mdx", "# Page\n<Comp />\n", "doc"),
    ],
)
def test_semantic_type(path: str, text: str, kind: str) -> None:
    assert _kind(path, text) == kind


def test_plugin_commands_folder_is_a_command(tmp_path: Path) -> None:
    (tmp_path / "plug" / ".claude-plugin").mkdir(parents=True)
    (tmp_path / "plug" / ".claude-plugin" / "plugin.json").write_text("{}")
    assert _kind("plug/commands/run.md", "Run it\n", repo_root=tmp_path) == "command"
    assert _kind("other/commands/run.md", "Run it\n", repo_root=tmp_path) == "doc"


def test_command_name_falls_back_to_the_file_name() -> None:
    st = _parse(".claude/commands/deploy.md", "Deploy the app\n").statements[0]
    assert st.name == "deploy" and st.description is None


def test_name_falls_back_to_title_then_file_name() -> None:
    assert _parse("docs/a.md", "# Getting started\n").statements[0].name == "Getting started"
    assert _parse("docs/a.md", "no heading\n").statements[0].name == "a.md"


# ── front matter ─────────────────────────────────────────────────────────────


def test_invalid_front_matter_falls_back_to_doc_and_is_flagged() -> None:
    rec = _parse("skills/x/SKILL.md", "---\nname: [unclosed\ndescription: d\n---\n# X\n")
    assert rec.metadata["kind"] == "doc" and rec.metadata["frontmatterError"] is True
    assert rec.metadata["frontmatter"] == {}
    assert rec.statements[0].semanticType == "doc"


def test_unclosed_front_matter_is_body_text() -> None:
    rec = _parse("docs/x.md", "---\nname: x\n# Title\n")
    assert rec.metadata["frontmatter"] == {} and "frontmatterError" not in rec.metadata


@pytest.mark.parametrize(
    "front_matter",
    [
        "name: &a [*a]\ndescription: d",  # recursive alias
        "a: &x [1,2]\nb: *x\nname: n\ndescription: d",  # any alias
        "name: x\ndescription: d\n" + "k: " + "v" * 9000,  # over the size cap
        "- just\n- a list",  # not a mapping
    ],
)
def test_unsafe_or_unusable_front_matter_is_rejected(front_matter: str) -> None:
    rec = _parse("skills/x/SKILL.md", f"---\n{front_matter}\n---\n# X\n")
    assert rec.metadata["frontmatterError"] is True and rec.metadata["kind"] == "doc"
    rec.model_dump_json()  # serializable: the sink can always write it


def test_front_matter_is_reduced_to_json_values() -> None:
    rec = _parse(
        "skills/x/SKILL.md",
        "---\nname: x\ndescription: d\ncreated: 2026-01-02\nblob: !!binary aGVsbG8=\n"
        "deep: {a: {b: {c: {d: 1}}}}\napi_token: abc123\n---\n",
    )
    fm = rec.metadata["frontmatter"]
    assert fm["created"] == "2026-01-02"
    assert "blob" not in fm  # bytes dropped
    assert fm["deep"] == {"a": {}}  # depth-capped at three mapping levels
    assert fm["api_token"] == "***"  # secret-named key
    rec.model_dump_json()


def test_crlf_and_bom() -> None:
    rec = _parse("skills/x/SKILL.md", "﻿---\r\nname: x\r\ndescription: d\r\n---\r\n# T\r\n")
    assert rec.metadata["kind"] == "skill" and "\r" not in rec.statements[0].text


def test_headings_inside_code_fences_are_not_outline() -> None:
    rec = _parse(
        "docs/a.md", "# Real\n```bash\n# comment, not a heading\n```\n## Also real\n#### deep\n"
    )
    assert [h["text"] for h in rec.metadata["headings"]] == ["Real", "Also real"]


def test_rst_title() -> None:
    rec = _parse("README.rst", "My Project\n==========\n\nUsage\n-----\n")
    assert rec.language == "rst" and rec.metadata["title"] == "My Project"
    assert [h["level"] for h in rec.metadata["headings"]] == [1, 2]


# ── secrets ──────────────────────────────────────────────────────────────────


def test_secrets_are_masked_and_prose_kept() -> None:
    text = (
        "# Setup\n"
        "Clone with https://alice:s3cr3t@github.com/org/repo.git then run it.\n"
        "```\nexport GITHUB_TOKEN=ghp_" + "a" * 36 + "\nDB_PASSWORD=changeme\n```\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nAAAA\n-----END RSA PRIVATE KEY-----\n"
        "Done.\n"
    )
    out = _parse("docs/setup.md", text).statements[0].text
    assert "s3cr3t" not in out and "https://***:***@github.com/org/repo.git" in out
    assert "ghp_" not in out and "changeme" not in out and "DB_PASSWORD=***" in out
    assert (
        "MIIEow" not in out
        and "-----BEGIN RSA PRIVATE KEY-----\n***\n-----END RSA PRIVATE KEY-----" in out
    )
    assert out.startswith("# Setup\nClone with") and out.rstrip().endswith("Done.")


def test_data_uri_payload_is_dropped() -> None:
    out = _parse("docs/a.md", "![x](data:image/png;base64," + "A" * 5000 + ")\n").statements[0].text
    assert out == "![x](data:image/png;base64,…)\n" or out.startswith(
        "![x](data:image/png;base64,…)"
    )


# ── limits ───────────────────────────────────────────────────────────────────


def test_text_over_64_kb_is_cut_on_a_line_boundary_and_marked() -> None:
    line = "x" * 99
    text = "\n".join([line] * 1000) + "\n"  # 100,000 bytes, 1000 lines
    rec = _parse("docs/big.md", text)
    st = rec.statements[0]
    body, _, marker = st.text.rpartition("\n")
    assert len(body.encode()) <= MAX_TEXT_BYTES
    assert body.split("\n")[-1] == line  # whole lines only
    assert st.endLine == len(body.split("\n")) < 1000
    assert marker.startswith("…[+") and "bytes dropped: markdown 64 KB cap]" in marker
    assert rec.metadata["truncated"] is True and rec.metadata["originalSize"] == len(text)


def test_a_single_line_over_64_kb_is_hard_cut() -> None:
    st = _parse("docs/one.md", "y" * (MAX_TEXT_BYTES + 10)).statements[0]
    assert st.endLine == 1 and st.text.startswith("y" * 100) and "bytes dropped" in st.text


def test_text_within_the_cap_is_whole() -> None:
    rec = _parse("docs/a.md", "a\nb\n")
    assert rec.statements[0].endLine == 2 and "truncated" not in rec.metadata


def test_file_over_512_kb_keeps_the_file_record_only() -> None:
    text = "# Big\n" + ("z" * 100 + "\n") * (MAX_STATEMENT_FILE_BYTES // 100)
    rec = _parse("docs/huge.md", text)
    assert rec.statements == []
    assert rec.metadata["truncated"] is True and rec.metadata["originalSize"] == len(text)
    assert rec.metadata["title"] == "Big"


def test_document_statements_are_not_split() -> None:
    from breezeai_cog.emit import split_oversized_statements

    st = _parse("docs/a.md", ("w" * 79 + "\n") * 500).statements  # 40,000 chars
    assert split_oversized_statements(st, 8000) == st


# ── pipeline ─────────────────────────────────────────────────────────────────


class _Sink:
    def __init__(self) -> None:
        self.records: list = []
        self.meta: ProjectMetaData | None = None

    def write(self, record) -> None:
        self.records.append(record)

    def finalize(self, meta) -> None:
        self.meta = meta


def _assemble(records, **kw) -> tuple[_Sink, ProjectMetaData]:
    sink = _Sink()
    meta = pipeline._assemble(Path("."), iter((r.language, r) for r in records), sink, **kw)
    return sink, meta


def test_docs_are_not_code_totals() -> None:
    _, meta = _assemble([_parse("docs/a.md", "a\nb\nc\n"), _parse("README.md", "# x\n")])
    assert meta.totalLinesOfCode == 0 and meta.analyzedLanguages == []
    assert meta.configs["byType"] == {"documentation": 2}


def test_only_typed_docs_make_a_docs_only_folder_worth_writing() -> None:
    _, meta = _assemble([_parse("README.md", "# x\n"), _parse("CHANGELOG.md", "# c\n")])
    assert not meta.has_content()
    _, meta = _assemble([_parse("skills/s/SKILL.md", SKILL)])
    assert meta.configs["capturedAgentDocuments"] == 1 and meta.has_content()


def test_untyped_docs_past_the_cap_keep_the_file_only() -> None:
    docs = [_parse(f"docs/{i}.md", f"# {i}\n") for i in range(3)]
    typed = _parse("CLAUDE.md", "# rules\n")
    sink, meta = _assemble([*docs, typed], max_markdown_statements=2)
    by_path = {r.path: r for r in sink.records}
    assert [len(by_path[f"docs/{i}.md"].statements) for i in range(3)] == [1, 1, 0]
    assert by_path["docs/2.md"].metadata["statementSkipped"] == "max_markdown_statements"
    assert len(by_path["CLAUDE.md"].statements) == 1  # typed docs are always kept
    assert meta.totalFiles == 4
