"""VueParser — a TypeScript framework parser selected (one parser per file) when
``claims`` sees a ``.vue`` file or a ``vue`` / ``vue-router`` import. Covers Vue 2 and
Vue 3 with a single parser: both use the ``.vue`` SFC format and the same ``vue-router``
route-array shape, so the version differences are branches inside route detection, not
separate parsers.

For ``.vue`` files it extracts the ``<script>`` block into a shadow source (see
``sfc.py``) and runs the full TS extraction on that; for ``.ts``/``.js`` files (a
``router/index.ts`` route config, an app-bootstrap file) it parses normally.

vue-router route detection is NOT done here — it lives in the base ``TypeScriptParser.
extract`` additive pass (beside ``detect_express``), because a Vue route array frequently
lives in a file this parser never claims (a default-export array, a ``router/modules/*``
fragment that imports nothing from vue-router). Running it additively on every TS/JS file
is the only way to catch those; ``.vue`` and vue-router files get it for free via
``extract`` too. See ``routes.detect_vue_routes``."""

from __future__ import annotations

from dataclasses import replace

from ...emit import file_id
from ...schemas import FileRecord, Statement
from ..base import ParseContext
from ..treesitter import parse_source
from ..typescript.parser import TypeScriptParser
from .components import mark_composables, mark_factory_ui_roles
from .sfc import script_grammar, script_language, script_ranges, shadow_source
from .template import collect_vue_template_statements

# Byte guards for a Vue-ecosystem import in a .ts/.js file: ``from 'vue'`` / ``from "vue"`` (the
# app and store modules), any ``vue-router`` reference (the router config), or ``'pinia'`` /
# ``"pinia"`` (the store lib — a Pinia store module often imports only pinia, not vue, yet is
# still Vue and must be tagged framework="vue" + get its defineStore uiRole). Quoted so the
# match is an import specifier, not the word appearing in a comment/string.
_VUE_IMPORT_GUARDS = (b"'vue'", b'"vue"', b"vue-router", b"'pinia'", b'"pinia"')


class VueParser(TypeScriptParser):
    name = "typescript-vue"
    # Add ``.vue`` to the inherited TS extensions so this parser is also a candidate for the
    # ``.ts``/``.js`` router/bootstrap files (where ``claims`` gates it on a vue import).
    extensions: tuple[str, ...] = (*TypeScriptParser.extensions, ".vue")
    priority = 10  # above the base TS parser; coexists per-file with other TS frameworks
    frameworks = ["vue"]
    # TS statement types + the ``<template>`` nodes this parser emits (capabilities honesty).
    statement_types = [*TypeScriptParser.statement_types, "directive_attribute", "interpolation"]

    def claims(self, path: str, source: bytes) -> bool:
        # A .vue SFC is always ours. A .ts/.js file is ours only when it imports vue /
        # vue-router (router configs, app bootstrap) — byte guard keeps it off unrelated TS.
        return path.endswith(".vue") or any(g in source for g in _VUE_IMPORT_GUARDS)

    def _template_statements(
        self, ctx: ParseContext, fid: str, seen_ids: set[str]
    ) -> list[Statement]:
        """Parse the SFC with the ``vue`` grammar and capture ``<template>`` bindings — the
        script capture uses the shadow source (which blanks the template), so this is the only
        pass that sees the markup. Closes the documented ``sfc.py`` gap."""
        vroot = parse_source("vue", ctx.source, ctx.parse_timeout_micros).root_node
        return collect_vue_template_statements(
            vroot,
            ctx.source,
            ctx.path,
            parent_id=fid,
            seen_ids=seen_ids,
            limit=ctx.statement_text_limit,
            emit_routes=not self.is_fixture_file(ctx.path),
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        if ctx.path.endswith(".vue"):
            ranges = script_ranges(ctx.source)
            if not ranges:  # a template/style-only SFC has no code — still capture the template
                fid = file_id(ctx.path)
                rec = FileRecord(
                    id=fid,
                    path=ctx.path,
                    type="code",
                    # language = the JS/TS axis (from <script lang>); a template-only SFC has
                    # no script → javascript. framework = the framework axis; uiRole = the role.
                    language=script_language(ctx.source),
                    framework="vue",
                    loc=0,
                    uiRole="component",  # a .vue SFC is a component even with no <script>
                )
                if ctx.capture_statements:
                    rec.statements.extend(self._template_statements(ctx, fid, set()))
                return rec
            grammar = script_grammar(ctx.source)
            # Parse the shadow (script bytes at their real offsets, everything else blanked);
            # feed the SAME shadow to extract()/route detection, since node byte offsets index
            # into it — and the script bytes are identical to the original there.
            parsed_source = shadow_source(ctx.source, ranges)
            parse_ctx = replace(ctx, source=parsed_source)
        else:
            parsed_source = ctx.source
            grammar = "tsx" if ctx.path.endswith((".tsx", ".jsx")) else "typescript"
            parse_ctx = ctx

        root = parse_source(grammar, parsed_source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, parse_ctx)  # runs detect_vue_routes in its additive pass
        # framework axis: every file this parser claims IS a Vue file (an SFC, or a .ts/.js
        # router/bootstrap that imports vue) — stamp it unconditionally so "list Vue files" is
        # answerable, not only when a route array happened to be detected. Authoritative over
        # any tentative label the additive extract pass set.
        record.framework = "vue"
        if ctx.path.endswith(".vue"):
            # language axis: the SFC's script lang (ts/js), NOT "vue" — "vue" is the framework,
            # which the extension can't carry, so JS/TS would otherwise be lost on .vue files.
            record.language = script_language(ctx.source)
            record.uiRole = "component"  # a .vue SFC is, by definition, a component
        # Factory-defined UI entities in any file this parser handles — Vue `defineComponent`
        # (component) / Pinia `defineStore` (store) — mark the File (default export) or the
        # binding's `lexical_declaration` statement.
        mark_factory_ui_roles(root, parsed_source, record)
        # Composables — a `useX` function that calls a Vue reactivity primitive.
        mark_composables(root, parsed_source, record)
        # <template> bindings — parsed from the ORIGINAL source with the vue grammar (the
        # shadow blanked the template). Closes the sfc.py gap; script statements keep their ids.
        if ctx.path.endswith(".vue") and ctx.capture_statements:
            record.statements.extend(
                self._template_statements(ctx, file_id(ctx.path), {s.id for s in record.statements})
            )
        return record
