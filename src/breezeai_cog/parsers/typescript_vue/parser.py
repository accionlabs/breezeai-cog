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
from ...schemas import FileRecord
from ..base import ParseContext
from ..treesitter import parse_source
from ..typescript.parser import TypeScriptParser
from .sfc import script_grammar, script_ranges, shadow_source

# Byte guards for a vue import in a .ts/.js file: ``from 'vue'`` / ``from "vue"`` (the app
# and store modules) or any ``vue-router`` reference (the router config).
_VUE_IMPORT_GUARDS = (b"'vue'", b'"vue"', b"vue-router")


class VueParser(TypeScriptParser):
    name = "typescript-vue"
    # Add ``.vue`` to the inherited TS extensions so this parser is also a candidate for the
    # ``.ts``/``.js`` router/bootstrap files (where ``claims`` gates it on a vue import).
    extensions: tuple[str, ...] = (*TypeScriptParser.extensions, ".vue")
    priority = 10  # above the base TS parser; coexists per-file with other TS frameworks
    frameworks = ["vue"]

    def claims(self, path: str, source: bytes) -> bool:
        # A .vue SFC is always ours. A .ts/.js file is ours only when it imports vue /
        # vue-router (router configs, app bootstrap) — byte guard keeps it off unrelated TS.
        return path.endswith(".vue") or any(g in source for g in _VUE_IMPORT_GUARDS)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        if ctx.path.endswith(".vue"):
            ranges = script_ranges(ctx.source)
            if not ranges:  # a template/style-only SFC has no code to capture
                return FileRecord(
                    id=file_id(ctx.path),
                    path=ctx.path,
                    type="code",
                    language="vue",
                    loc=0,
                    uiRole="component",  # a .vue SFC is a component even with no <script>
                )
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
        if ctx.path.endswith(".vue"):
            record.language = (
                "vue"  # an SFC is a Vue file, not a bare .ts (matches empty-SFC label)
            )
            record.uiRole = "component"  # a .vue SFC is, by definition, a component
        return record
