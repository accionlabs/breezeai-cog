"""MarkdownParser: one File + one whole-file ``document`` statement per
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
        (
            "agents/reviewer.md",
            "---\nname: reviewer\ndescription: Reviews code\ntools: Read\n---\n",
            "agent",
        ),
        # Docusaurus / VitePress front matter: name + description alone is not an agent.
        ("docs/agents/overview.md", "---\nname: overview\ndescription: About agents\n---\n", "doc"),
        (".claude/agents/r.md", "---\nname: r\ndescription: d\n---\n", "agent"),
        ("agents/notes.md", "# notes without front matter\n", "doc"),
        (".github/agents/planner.agent.md", "# planner\n", "agent"),
        (".claude/commands/deploy.md", "Deploy the app\n", "command"),
        (
            "plugin/commands/x.md",
            "---\ndescription: does x\nargument-hint: <file>\n---\n",
            "command",
        ),
        # A CLI reference page with an SEO description is documentation, not a slash command.
        ("website/docs/cli/commands/build.md", "---\ndescription: Build the site\n---\n", "doc"),
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
        # Instruction file names match in their exact case: lower-case ones are prose.
        ("CLAUDE.local.md", "# rules\n", "agent_instructions"),
        ("docs/providers/claude.md", "# Claude provider\n", "doc"),
        ("docs/agents.md", "# Agents\n", "doc"),
        ("Gemini.md", "# Gemini\n", "doc"),
        # A decisions folder's index and template are not decisions.
        ("docs/adr/index.md", "# Decisions\n", "doc"),
        ("docs/adr/template.md", "# Title\n## Status\n## Decision\n", "doc"),
        ("docs/decisions/0000-adr-template.md", "# Title\n", "doc"),
        # A bare `adr/` folder needs the numbered name or the Status + Decision headings.
        ("adr/0003-queue.md", "# Queue\n", "adr"),
        ("adr/queue.md", "# Queue\n## Status\nAccepted\n## Decision\nUse it\n", "adr"),
        ("adr/overview.md", "# About our ADRs\n", "doc"),
        ("tools/adr/index.md", "# Index\n", "doc"),
        ("adr/template.md", "# T\n## Status\n## Decision\n", "doc"),
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
        and "-----BEGIN RSA PRIVATE KEY-----\n***\n***\n-----END RSA PRIVATE KEY-----" in out
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


def test_untyped_docs_past_the_byte_budget_keep_the_file_only() -> None:
    docs = [_parse(f"docs/{i}.md", "x" * 99 + "\n") for i in range(3)]
    typed = _parse("CLAUDE.md", "y" * 500 + "\n")
    sink, meta = _assemble([*docs, typed], max_markdown_bytes=250)
    by_path = {r.path: r for r in sink.records}
    assert [len(by_path[f"docs/{i}.md"].statements) for i in range(3)] == [1, 1, 0]
    assert by_path["docs/2.md"].metadata["statementSkipped"] == "max_markdown_bytes"
    assert len(by_path["CLAUDE.md"].statements) == 1  # typed docs are always kept
    assert meta.configs["markdownCapped"] == 1


def test_byte_budget_follows_path_order_and_file_size(tmp_path: Path) -> None:
    from breezeai_cog.core.scanner import ScanEntry

    sizes = {"a.md": 100, "a1.md": MAX_STATEMENT_FILE_BYTES + 1, "b.md": 100, "c.md": 10}
    for name, size in sizes.items():
        (tmp_path / name).write_bytes(b"x" * size)
    entries = [ScanEntry(p, "markdown") for p in reversed(sorted(sizes))]
    # a1.md keeps no statement, so it costs nothing; c.md is past the budget even though it
    # would fit on its own: the docs kept are a prefix in path order.
    assert pipeline._markdown_skips(entries, 0, 150, tmp_path) == {
        "b.md": "max_markdown_bytes",
        "c.md": "max_markdown_bytes",
    }
    assert pipeline._markdown_skips(entries, 2, 1000, tmp_path) == {
        "b.md": "max_markdown_statements",
        "c.md": "max_markdown_statements",
    }
    assert pipeline._markdown_skips(entries, 0, 0, tmp_path) is None


def test_front_matter_secrets_are_masked_in_the_text() -> None:
    text = (
        "---\nname: x\ndescription: d\napi_token: abc123\npassword: 'hunter2'\n"
        "credentials:\n  user: bob\n  key: zzz999\nmodel: opus\n---\nBody\n"
    )
    record = _parse("skills/x/SKILL.md", text)
    st = record.statements[0]
    for secret in ("abc123", "hunter2", "zzz999"):
        assert secret not in st.text
    assert "api_token: ***" in st.text and "model: opus" in st.text and "Body" in st.text
    assert record.metadata["frontmatter"]["api_token"] == "***"


def test_headings_title_and_name_are_redacted() -> None:
    token = "ghp_" + "a" * 36
    record = _parse("docs/setup.md", f"# Setup with {token}\n## Use https://bob:pw@host/x\n")
    assert token not in record.metadata["title"]
    assert all(
        token not in h["text"] and "bob:pw" not in h["text"] for h in record.metadata["headings"]
    )
    assert token not in record.statements[0].name


def test_adversarial_lines_parse_in_linear_time() -> None:
    import time

    start = time.perf_counter()
    _parse("docs/a.md", "TOKEN" * 100_000 + "\n" + "# a" + " " * 200_000 + "x\n")
    assert time.perf_counter() - start < 2.0


def test_env_secret_still_masked_and_non_secret_kept() -> None:
    st = _parse("docs/a.md", "export DB_PASSWORD=hunter2\nPATH_PREFIX=/usr\n").statements[0]
    assert "hunter2" not in st.text and "DB_PASSWORD=***" in st.text
    assert "PATH_PREFIX=/usr" in st.text


def test_doc_cap_follows_path_order_not_arrival_order() -> None:
    from breezeai_cog.core.scanner import ScanEntry

    paths = [f"docs/{i}.md" for i in range(3)]
    skips = pipeline._markdown_skips([ScanEntry(p, "markdown") for p in reversed(paths)], 2)
    for order in (paths, list(reversed(paths))):
        sink, _ = _assemble(
            [_parse(p, "# x\n") for p in order], max_markdown_statements=2, markdown_skips=skips
        )
        kept = sorted(r.path for r in sink.records if r.statements)
        assert kept == ["docs/0.md", "docs/1.md"]


# ── review fixes ─────────────────────────────────────────────


def test_pem_header_without_footer_masks_no_prose() -> None:
    from breezeai_cog.parsers.structured_json.redaction import redact_text

    text = "intro\n-----BEGIN PRIVATE KEY----- is the header\n## Next section\nmore text here"
    assert redact_text(text) == text


def test_unterminated_pem_masks_only_the_key_lines() -> None:
    from breezeai_cog.parsers.structured_json.redaction import redact_text

    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\nAAAA==\n"
    text = "```\n-----BEGIN RSA PRIVATE KEY-----\n" + body + "```\n## Next\nprose\n"
    out = redact_text(text)
    assert "MIIEvQ" not in out and "AAAA==" not in out
    assert out == "```\n-----BEGIN RSA PRIVATE KEY-----\n***\n***\n```\n## Next\nprose\n"


def test_pem_footer_after_another_header_is_not_the_match() -> None:
    from breezeai_cog.parsers.structured_json.redaction import redact_text

    text = (
        "-----BEGIN PRIVATE KEY----- quoted in prose\nkeep this\n"
        "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n"
    )
    out = redact_text(text)
    assert "keep this" in out and "MIIE" not in out


def test_one_line_pem_in_a_string_literal_is_masked() -> None:
    from breezeai_cog.parsers.structured_json.redaction import redact_text

    out = redact_text('k = "-----BEGIN PRIVATE KEY-----\\nMIIE\\n-----END PRIVATE KEY-----"')
    assert out == 'k = "-----BEGIN PRIVATE KEY-----***-----END PRIVATE KEY-----"'


def test_pem_mask_keeps_the_line_count_so_end_line_is_the_file_line() -> None:
    key = (
        "-----BEGIN PRIVATE KEY-----\n"
        + "\n".join(["A" * 64] * 20)
        + "\n-----END PRIVATE KEY-----\n"
    )
    filler = ("x" * 99 + "\n") * 700
    rec = _parse("docs/k.md", key + filler)
    st = rec.statements[0]
    body = st.text.rpartition("\n")[0]
    assert st.endLine == len(body.split("\n"))
    assert body.split("\n")[21] == "-----END PRIVATE KEY-----"  # line 22 in the file too


def test_long_scheme_like_run_parses_fast() -> None:
    import time

    start = time.perf_counter()
    rec = _parse("docs/a.md", "a" * 300_000)  # under 512 KB: goes through the cleaner
    assert time.perf_counter() - start < 2.0
    assert rec.statements and rec.metadata["truncated"] is True


@pytest.mark.parametrize(
    ("path", "text", "kind"),
    [
        ("rules/breeze-guard.mdc", "---\ndescription: guard\n---\nRules\n", "agent_instructions"),
        ("any/where/x.mdc", "Rules\n", "agent_instructions"),
        (".cursor/commands/review.md", "Review the diff\n", "command"),
        ("docs/review.prompt.md", "A template\n", "doc"),
    ],
)
def test_cursor_rules_and_commands(path: str, text: str, kind: str) -> None:
    assert _kind(path, text) == kind


def test_cursor_plugin_layout(tmp_path: Path) -> None:
    # breezeai-claude-plugin: a `.cursor-plugin/plugin.json` root with `rules/*.mdc` and
    # `commands/*.md`.
    (tmp_path / ".cursor-plugin").mkdir()
    (tmp_path / ".cursor-plugin" / "plugin.json").write_text("{}")
    assert _kind("rules/breeze-guard.mdc", "Guard\n", repo_root=tmp_path) == "agent_instructions"
    assert _kind("commands/search.md", "Search it\n", repo_root=tmp_path) == "command"


def test_cursor_front_matter_with_unquoted_glob_keeps_name_and_description() -> None:
    rec = _parse(
        "skills/x/SKILL.md",
        "---\nname: x\ndescription: Lint TS\nglobs: **/*.ts\nalwaysApply: false\n---\n# X\n",
    )
    assert rec.metadata["frontmatterError"] is True
    assert rec.metadata["frontmatter"] == {"name": "x", "description": "Lint TS"}
    assert rec.metadata["kind"] == "skill" and rec.statements[0].description == "Lint TS"


def test_non_yaml_leading_block_keeps_its_headings() -> None:
    rec = _parse("docs/a.md", "---\n# Title\nSome prose here\n---\n## Next\n")
    assert rec.metadata["frontmatterError"] is True
    assert [h["text"] for h in rec.metadata["headings"]] == ["Title", "Next"]


def test_utf16_document_is_decoded() -> None:
    rec = _parse("docs/a.md", "# Título\nbody\n".encode("utf-16"))  # BOM-prefixed
    assert rec.metadata["title"] == "Título" and rec.statements[0].text == "# Título\nbody\n"


@pytest.mark.parametrize(
    ("line", "secret"),
    [
        ("DB_PASS=hunter2", "hunter2"),
        ("MYSQL_ROOT_PASS=hunter2", "hunter2"),
        ("AWS_ACCESS_KEY=AKXYZ", "AKXYZ"),
        ("OAUTH_CLIENT_KEY=ck123", "ck123"),
        ("GCP_CREDENTIALS=/etc/creds", "/etc/creds"),
        ('DB_PASSWORD="two words"', "two words"),
    ],
)
def test_more_env_secret_names_are_masked(line: str, secret: str) -> None:
    st = _parse("docs/a.md", f"{line}\n").statements[0]
    assert secret not in st.text and "***" in st.text


def test_env_lookalikes_are_kept() -> None:
    text = "PASSPORT_URL=/p\nBYPASS_CACHE=1\nMAX_TOKENS=4096\n"
    assert _parse("docs/a.md", text).statements[0].text == text


@pytest.mark.parametrize(
    "line",
    [
        "password: hunter2",
        "spring.datasource.password=hunter2",
        '{"apiKey": "hunter2", "region": "us"}',
        "  client_secret = 'hunter2'",
    ],
)
def test_secret_keys_in_code_blocks_are_masked(line: str) -> None:
    st = _parse("docs/a.md", f"# Config\n```\n{line}\n```\n").statements[0]
    assert "hunter2" not in st.text and "***" in st.text


def test_code_block_non_secrets_are_kept() -> None:
    text = (
        "```ts\ninterface U { password: string; }\nmax_tokens: 1024\nregion: us\n```\n"
        "Outside a fence, password: prose stays.\n"
    )
    assert _parse("docs/a.md", text).statements[0].text == text


def test_front_matter_block_scalar_secret_is_masked() -> None:
    text = "---\nname: x\napi_token: |\n  line-one-secret\n  line-two-secret\nmodel: opus\n---\nB\n"
    st = _parse("docs/a.md", text).statements[0]
    assert "line-one-secret" not in st.text and "line-two-secret" not in st.text
    assert "model: opus" in st.text


# ── review fixes, round 2 ────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer abcdefghijklmnop\n",
        "```\nAuthorization: Bearer abcdefghijklmnop\n```\n",
        '```bash\ncurl -H "Authorization: Bearer abcdefghijklmnop" https://api\n```\n',
        "curl -H 'Proxy-Authorization: Basic abcdefghijklmnop' https://api\n",
        "```\nX-API-Key: Key abcdefghijklmnop\n```\n",
        "```\nAuthorization: abcdefghijklmnop\n```\n",
        "```\nheaders = {'Authorization': 'Bearer abcdefghijklmnop'}\n```\n",
    ],
)
def test_authorization_scheme_and_credential_are_masked(text: str) -> None:
    out = _parse("docs/a.md", text).statements[0].text
    assert "abcdefghijklmnop" not in out and "Bearer" not in out and "***" in out


def test_authorization_prose_without_a_scheme_is_kept() -> None:
    text = "Authorization: required for every call.\n"
    assert _parse("docs/a.md", text).statements[0].text == text


@pytest.mark.parametrize(
    "text",
    [
        # MkDocs: a fence nested under a list item, four spaces in.
        "1. Configure\n\n    ```yaml\n    password: hunter2\n    ```\n",
        "- step\n\n        ```\n        client_secret = 'hunter2'\n        ```\n",
        # A plain indented code block.
        "Config:\n\n    db_password = hunter2\n",
        "Config:\n\n\tapi_key: hunter2\n",
    ],
)
def test_indented_fences_and_indented_code_are_masked(text: str) -> None:
    out = _parse("docs/a.md", text).statements[0].text
    assert "hunter2" not in out and "***" in out


def test_a_quoted_shorter_fence_does_not_close_the_block() -> None:
    text = "````markdown\n```python\nx = 1\n```\npassword = hunter2\n````\n"
    assert "hunter2" not in _parse("docs/a.md", text).statements[0].text


def test_indented_fence_masking_parses_in_linear_time() -> None:
    import time

    start = time.perf_counter()
    _parse("docs/a.md", " " * 200_000 + "`" * 100_000 + "\n    " + "a" * 200_000 + "=\n")
    assert time.perf_counter() - start < 2.0


def test_front_matter_flow_mapping_secrets_are_masked() -> None:
    text = (
        "---\nname: x\nenv: {API_TOKEN: tok123abc, DB_PASSWORD: hunter2xyz, region: us}\n"
        "items: [{token: abc999}, plain]\nkeys: {tokens: [aa111, bb222], ok: 1}\n---\nBody\n"
    )
    rec = _parse("docs/a.md", text)
    out = rec.statements[0].text
    for secret in ("tok123abc", "hunter2xyz", "abc999", "aa111", "bb222"):
        assert secret not in out
    assert "region: us" in out and "ok: 1" in out and "plain" in out
    assert rec.metadata["frontmatter"]["env"] == {
        "API_TOKEN": "***",
        "DB_PASSWORD": "***",
        "region": "us",
    }


def test_one_secret_name_rule_for_front_matter_env_and_code() -> None:
    text = (
        "---\nname: x\nDB_PASS: s1secret\nmysql_pass: s2secret\npassage: keep1\n"
        "compass: keep2\nbypass: keep3\n---\n"
        "```\nmysql_pass = s3secret\nredis-pass: s4secret\ncompass = keep4\n```\n"
        "export MYSQL_PASS=s5secret\nBYPASS_CACHE=keep5\n"
    )
    rec = _parse("docs/a.md", text)
    fm, out = rec.metadata["frontmatter"], rec.statements[0].text
    assert fm["DB_PASS"] == "***" and fm["mysql_pass"] == "***"
    assert (fm["passage"], fm["compass"], fm["bypass"]) == ("keep1", "keep2", "keep3")
    for n in range(1, 6):
        assert f"s{n}secret" not in out and f"keep{n}" in out


def test_commands_folder_deeper_in_a_plugin_is_a_doc(tmp_path: Path) -> None:
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{}")
    agent = "---\nname: a\ndescription: d\n---\n"
    assert _kind("commands/deploy.md", "Deploy\n", repo_root=tmp_path) == "command"
    assert _kind("agents/a.md", agent, repo_root=tmp_path) == "agent"
    assert _kind("docs/commands/cli.md", "CLI\n", repo_root=tmp_path) == "doc"
    assert _kind("skills/a/references/commands/deploy.md", "x\n", repo_root=tmp_path) == "doc"
    assert _kind("docs/agents/a.md", agent, repo_root=tmp_path) == "doc"


def test_readme_and_changelog_count_against_the_doc_cap() -> None:
    docs = [_parse(p, "# x\n") for p in ("CHANGELOG.md", "README.md", "docs/a.md")]
    agents = [_parse("CLAUDE.md", "# rules\n"), _parse("skills/s/SKILL.md", SKILL)]
    sink, meta = _assemble([*docs, *agents], max_markdown_statements=1)
    kept = {r.path for r in sink.records if r.statements}
    assert kept == {"CHANGELOG.md", "CLAUDE.md", "skills/s/SKILL.md"}
    assert meta.configs["capturedAgentDocuments"] == 2


def test_truncated_text_with_its_marker_fits_the_cap() -> None:
    for text in ("\n".join(["x" * 99] * 1000) + "\n", "y" * (MAX_TEXT_BYTES + 10)):
        st = _parse("docs/big.md", text).statements[0]
        assert "bytes dropped" in st.text and len(st.text.encode()) <= MAX_TEXT_BYTES


def test_code_lookups_are_not_masked_but_literals_are() -> None:
    kept = (
        "password = os.environ['DB_PASSWORD']\ntoken = get_token()\n"
        "secret = settings.CLIENT_SECRET\napi_key: process.env.API_KEY\n"
    )
    text = f"```python\n{kept}password = hunter2\napi_key = 'hunter3'\n```\n"
    out = _parse("docs/a.md", text).statements[0].text
    assert kept in out
    assert "hunter2" not in out and "hunter3" not in out


def test_pgp_private_key_block_is_masked_line_by_line() -> None:
    text = (
        "```\n-----BEGIN PGP PRIVATE KEY BLOCK-----\nVersion: GnuPG v2\n\n"
        "lQOYBF0AAAAAAAAAAAAAAAAAAAAAAAAAA\n=abCD\n-----END PGP PRIVATE KEY BLOCK-----\n```\n"
    )
    out = _parse("docs/a.md", text).statements[0].text
    assert "lQOYBF0" not in out and "=abCD" not in out and "GnuPG" not in out
    assert out.count("\n") == text.count("\n")
    assert "-----END PGP PRIVATE KEY BLOCK-----" in out


def test_openai_legacy_key_in_prose_is_masked() -> None:
    key = "sk-" + "Ab1" * 16
    out = _parse("docs/a.md", f"Set the key to {key} before running.\n").statements[0].text
    assert key not in out and "Set the key to *** before running." in out


K8S_ENV = """```yaml
env:
  - name: DB_PASSWORD
    value: s3cr3t1
  - name: LOG_LEVEL
    value: debug
  - value: "s3cr3t2"
    name: "API_TOKEN"
  - name: DB_HOST
    value: db.internal
```
"""


def test_sibling_name_value_secret_is_masked_in_a_fenced_block() -> None:
    out = _parse("docs/deploy.md", K8S_ENV).statements[0].text
    assert "s3cr3t1" not in out and "s3cr3t2" not in out
    assert "    value: ***" in out and '  - value: "***"' in out
    assert "value: debug" in out and "value: db.internal" in out
    assert "name: DB_PASSWORD" in out  # the name is not the secret


def test_sibling_name_value_outside_a_fence_is_prose() -> None:
    text = "- name: DB_PASSWORD\n  value: shown in the docs\n"
    assert "shown in the docs" in _parse("docs/a.md", text).statements[0].text


def test_nested_value_under_a_secret_name_is_not_a_sibling() -> None:
    text = "```yaml\n- name: API_TOKEN\n  meta:\n    value: keep1\n  other: keep2\n```\n"
    out = _parse("docs/a.md", text).statements[0].text
    assert "keep1" in out and "keep2" in out


def test_sibling_name_value_secret_is_masked_in_front_matter() -> None:
    text = (
        "---\nname: deploy\nenv:\n  - name: DB_PASSWORD\n    value: s3cr3t3\n"
        "  - name: REGION\n    value: us-east-1\n---\nBody\n"
    )
    rec = _parse("docs/a.md", text)
    out = rec.statements[0].text
    assert "s3cr3t3" not in out and "value: us-east-1" in out
    assert rec.metadata["frontmatter"]["env"] == [
        {"name": "DB_PASSWORD", "value": "***"},
        {"name": "REGION", "value": "us-east-1"},
    ]
    assert rec.statements[0].name == "deploy"
