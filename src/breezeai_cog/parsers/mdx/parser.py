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

#: What an ESM block may hold: MDX compiles a block only when its every statement is an
#: ``import`` or an ``export``.
_ESM_NODES = frozenset({"import_statement", "export_statement", "comment"})

log = get_logger("breezeai_cog.parsers.mdx")


def is_esm(code: bytes, timeout: int = 0) -> bool:
    """Whether ``code`` is an ESM block MDX compiles: it parses without error and holds only
    ``import`` / ``export`` statements. Any other statement is prose MDX would reject — a
    ``~~~js`` fence glued under an import parses as an expression — so the block is read as
    text and the fence it opens is still seen."""
    try:
        root = parse_source(_GRAMMAR, code, timeout).root_node
    except ValueError:  # parse timeout: not code we can read
        return False
    return not root.has_error and all(n.type in _ESM_NODES for n in root.named_children)


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

        def parses(code: bytes) -> bool:
            return is_esm(code, timeout)

        def budget_spent() -> None:
            # The scan stopped reading ESM here: imports after this point may be missing.
            log.warning("mdx.esm_parse_budget_reached", path=ctx.path, budget=MAX_PARSES_PER_FILE)

        shadow = esm_shadow(ctx.source, parses, budget_spent)
        try:
            root = parse_source(_GRAMMAR, shadow, timeout).root_node
            broken = [(n.start_byte, n.end_byte) for n in root.children if n.has_error]
            if broken:
                shadow = blank_ranges(shadow, broken)
                root = parse_source(_GRAMMAR, shadow, timeout).root_node
                if root.has_error:
                    # Blanking moved the error instead of removing it: no import is read from
                    # a tree that does not parse.
                    shadow = blank_ranges(shadow, [(0, len(shadow))])
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
