"""HtmlParser — the framework-neutral language parser owning ``.html`` / ``.htm``.

``.html`` is not one framework: it hosts Angular / AngularJS / Aurelia / Knockout / Thymeleaf
templates (each with its own binding dialect) and plain static pages. So this parser owns the
*file type* and stays framework-agnostic; the framework is **detected, never assumed**, in two
tiers:

1. **Cross-file resolver** (authoritative — see :mod:`.index`): a component's ``templateUrl``
   resolved to this file. Gives the framework **and** an ``importFiles`` link to the component.
2. **In-file fingerprint** (fallback — see :mod:`.fingerprint`): the markup's own uniquely-
   identifying binding syntax (``*ngFor`` → angular, ``ng-*`` → angularjs, ``v-*`` → vue,
   ``th:`` → thymeleaf, …). Names the framework but has **no** owner link. Ambiguous or
   unknown markup → ``None`` (honest-null).

Either tier makes the file a template (``uiRole="template"`` + ``framework``); the markup is
then parsed into statements only when a grammar for that framework exists
(:data:`_GRAMMAR_BY_FRAMEWORK` — Angular today), else honest-null (no wrong-grammar parse).
A ``.html`` that matches neither tier is a plain html File — never claimed as a template — so
the graph isn't flooded with markup that has no component behind it.

**Behavior libraries** (htmx / Alpine / Stimulus) are progressive enhancement layered on top
of whatever renders the page — not a rendering framework, so they are never named as the
framework: a Django-or-Thymeleaf page with htmx is still that framework. What they do that the
graph needs is recorded as statements: each htmx request attribute is an ``api_call``
(``html_htmx``). Alpine and Stimulus only act in the browser and record nothing.

P1 parses **Angular 2+** templates (the dedicated ``angular`` grammar in
``tree_sitter_language_pack`` — no new dependency). ``.aspx``/``.cshtml`` (HTML + server code
islands) are a later phase and a different technique (shadow-source), not this parser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..comments_common import comment_statements_for
from ..treesitter import parse_source
from ..html_htmx.calls import detect_htmx_calls
from .angularjs import read_angularjs_events
from .fingerprint import detect_rendering_framework
from .index import HtmlTemplateIndex, build_html_template_index
from .mappings import COMMENT_TYPES, FRAMEWORKS, STATEMENT_TYPES
from .statements import collect_template_statements
from ..template_common.markup import read_page_markup
from ..template_common.scripts import link_page_scripts, read_page_scripts
from ..typescript_vue.template import collect_vue_template_statements

#: framework → the tree-sitter grammar that models its template dialect. A framework absent
#: here is still tagged (role + framework) but its markup is not parsed into statements
#: (honest-null — no wrong-grammar statements). Extended as later grammars are added.
_GRAMMAR_BY_FRAMEWORK: dict[str, str] = {"angular": "angular"}


class HtmlParser(BaseParser):
    name = "html"
    extensions: tuple[str, ...] = (".html", ".htm")
    template_extensions: tuple[str, ...] = (".html", ".htm")  # markup — gated by --capture-templates
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def build_index(
        self, repo_root: Path, files: Sequence[Path], jobs: int = 1
    ) -> HtmlTemplateIndex:
        # Scans component sources for template references → maps each resolved .html to its
        # owning component AND the framework that resolved it.
        return build_html_template_index(Path(repo_root), files, jobs)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        fid = file_id(ctx.path)
        text = ctx.source.decode("utf-8", "replace")
        loc = count_loc(text)
        index = ctx.resolution_index
        component = index.component_for(ctx.path) if isinstance(index, HtmlTemplateIndex) else None
        # Framework: the cross-file resolver is authoritative (it also gives the component link);
        # the in-file fingerprint is the fallback (framework only, no owner link). Ambiguous or
        # unknown → None (honest-null).
        if component is not None:
            framework: str | None = component.framework
            import_files = [component.path]
        else:
            framework = detect_rendering_framework(text)
            import_files = []

        if framework is None:
            # Not a recognized template — a plain html File (it may still make htmx / script calls).
            plain = self._file(fid, ctx.path, loc)
            self._page(plain, ctx)
            return plain

        record = self._file(
            fid,
            ctx.path,
            loc,
            uiRole="template",
            framework=framework,
            import_files=import_files,
        )
        grammar = _GRAMMAR_BY_FRAMEWORK.get(framework)
        if ctx.capture_statements and component is not None and framework == "vue":
            # Markup of a Vue SFC's `<template src>`: read as that component's template.
            vroot = parse_source("vue", ctx.source, ctx.parse_timeout_micros).root_node
            record.statements.extend(
                collect_vue_template_statements(
                    vroot,
                    ctx.source,
                    ctx.path,
                    parent_id=fid,
                    seen_ids=set(),
                    limit=ctx.statement_text_limit,
                    emit_routes=not self.is_fixture_file(ctx.path),
                    whole_file=True,
                )
            )
        elif ctx.capture_statements and grammar is not None:
            root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
            seen_ids: set[str] = set()
            record.statements.extend(
                collect_template_statements(
                    root,
                    ctx.source,
                    ctx.path,
                    parent_id=fid,
                    seen_ids=seen_ids,
                    limit=ctx.statement_text_limit,
                    framework=framework,
                    emit_routes=not self.is_fixture_file(ctx.path),  # routes skip fixtures
                )
            )
            record.statements.extend(
                comment_statements_for(
                    root,
                    ctx.source,
                    ctx.path,
                    file_id=fid,
                    functions=[],
                    classes=[],
                    statements=record.statements,
                    control_flow=frozenset(),
                    comment_types=COMMENT_TYPES,
                    limit=ctx.statement_text_limit,
                    seen_ids=seen_ids,
                )
            )
        if ctx.capture_statements and framework == "angularjs":
            read_angularjs_events(record, ctx.source, ctx.path, ctx.parse_timeout_micros)
        self._page(record, ctx)
        return record

    def _page(self, record: FileRecord, ctx: ParseContext) -> None:
        # What every page has, whatever renders it: its script files as file links; its links
        # and form posts; htmx request attributes; and what every script detector (jQuery,
        # fetch, axios, client routers, …) finds inside the inline <script> blocks — the markup
        # grammar never looks there.
        link_page_scripts(record, ctx)
        if ctx.capture_statements:
            read_page_markup(record, ctx.source, ctx.path, ctx.parse_timeout_micros,
                             links=not self.is_fixture_file(ctx.path))
            detect_htmx_calls(record, ctx.source, ctx.path, ctx.parse_timeout_micros)
            read_page_scripts(record, ctx)

    @staticmethod
    def _file(
        fid: str,
        path: str,
        loc: int,
        *,
        uiRole: str | None = None,
        framework: str | None = None,
        import_files: list[str] | None = None,
    ) -> FileRecord:
        """Build the html FileRecord."""
        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="html",
            loc=loc,
            uiRole=uiRole,
            framework=framework,
            importFiles=import_files or [],
        )
