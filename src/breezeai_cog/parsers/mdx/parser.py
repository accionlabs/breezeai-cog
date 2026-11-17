"""MdxParser — the **language parser** owning ``.mdx`` files (Markdown with JSX).

An MDX file is recorded as ``language: "mdx"``, ``type: "code"``, with the files its ESM
``import`` lines bring in (``importFiles`` / ``externalImports``) and what it ``export``s::

    import { Chart } from '../components/Chart'     → importFiles ["components/Chart.tsx"]
    export const meta = { title: 'Sales' }          → exports ["meta"]

    # Sales report
    <Chart values={[1, 2, 3]} />

Only the ESM blocks are code (:mod:`.esm`); they are parsed with the ``tsx`` grammar in a
shadow of the file (everything else blanked, so lines and columns are the file's own) and read
by the TypeScript import extraction — relative paths, ``index`` files and the tsconfig / Vite
aliases resolve exactly as they do from a ``.tsx`` file. The aliases come from the TypeScript
index (``related_languages``); without it only relative imports resolve and the rest stay
external. A top-level statement that does not parse (prose that starts with ``import``, a cut-off
block) is dropped, never read as an import. The Markdown content itself is not captured, and
``loc`` counts only the ESM lines that parse — prose is not lines of code.

This parser alone owns ``.mdx``, so an MDX document (a ``README.mdx``, a Docusaurus or Nextra
page) is a code file with its imports, not a Markdown ``doc`` / ``readme`` / ``changelog``
statement: its title, front matter and headings are not captured.

The file's framework detectors run on it (``run_additive("mdx", …)``) — a framework that serves
``.mdx`` files as pages registers one with a ``Gate("mdx", …)`` (Next.js:
``parsers/typescript_nextjs/mdx_pages.py``).
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from ...emit import file_id
from ...logging import get_logger
from ...schemas import SCHEMA_VERSION, FileRecord
from ...utils import count_loc
from ..additive import run_additive
from ..base import BaseParser, ParseContext
from ..treesitter import parse_source
from ..typescript.imports import extract_imports
from .esm import MAX_PARSES_PER_FILE, blank_ranges, esm_shadow

#: The ESM blocks are JavaScript with JSX (``export const x = <A />``); TypeScript syntax in
#: them parses too.
_GRAMMAR = "tsx"

log = get_logger("breezeai_cog.parsers.mdx")


class MdxParser(BaseParser):
    name = "mdx"
    extensions: tuple[str, ...] = (".mdx",)
    schema_version = SCHEMA_VERSION
    #: The tsconfig / Vite aliases and the Next.js facts its detectors read.
    related_languages = ("typescript",)
    statement_types = ["route"]
    frameworks = ["nextjs"]

    def matches(self, path: str | Path) -> bool:
        """The base suffix gate, and ``.mdx`` in any case (``Guide.MDX``): no other parser
        registers it."""
        return super().matches(path) or PurePosixPath(path).suffix.lower() == ".mdx"

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        timeout = ctx.parse_timeout_micros
        probes = 0

        def parses(code: bytes) -> bool:
            nonlocal probes
            probes += 1
            try:
                return not parse_source(_GRAMMAR, code, timeout).root_node.has_error
            except ValueError:  # parse timeout: not code we can read
                return False

        shadow = esm_shadow(ctx.source, parses)
        if probes >= MAX_PARSES_PER_FILE:
            # The scan stopped reading ESM here: imports after this point may be missing.
            log.warning("mdx.esm_parse_budget_reached", path=ctx.path, budget=MAX_PARSES_PER_FILE)
        try:
            root = parse_source(_GRAMMAR, shadow, timeout).root_node
            broken = [(n.start_byte, n.end_byte) for n in root.children if n.has_error]
            if broken:
                shadow = blank_ranges(shadow, broken)
                root = parse_source(_GRAMMAR, shadow, timeout).root_node
        except ValueError:
            # Parse timeout: the file is still an MDX file, its imports are unknown (honest
            # null). Its detectors read the path, so they run on an empty tree.
            shadow = b""
            root = parse_source(_GRAMMAR, shadow).root_node

        internal, external, exports, _ = extract_imports(
            root, shadow, ctx.path, ctx.repo_root, ctx.related_indexes.get("typescript")
        )
        record = FileRecord(
            id=file_id(ctx.path),
            path=ctx.path,
            type="code",
            language="mdx",
            # Only the ESM code counts: an MDX page's prose is a document, not lines of code.
            loc=count_loc(shadow.decode("utf-8", "replace")),
            importFiles=[m for m in internal if m],
            externalImports=[m for m in external if m],
            exports=exports,
        )
        run_additive("mdx", root, ctx, record, is_fixture=self.is_fixture_file(ctx.path))
        return record
