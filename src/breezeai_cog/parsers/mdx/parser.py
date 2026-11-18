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
block) is dropped, never read as an import. ``loc`` counts only the ESM lines that parse — prose
is not lines of code. The ESM code is read for its imports and export names only: an ``export
function`` gets no Function record.

There is no MDX grammar, and tree-sitter-markdown (CommonMark: indented code blocks, no JSX)
would misread MDX, so the block structure — fences, JSX tags, ``{…}`` expressions, code spans,
HTML comments — is found by a scanner (:mod:`.esm`); only the ESM code is parsed by tree-sitter.

The prose is a Markdown document, read by the Markdown parser's
:func:`~breezeai_cog.parsers.markdown.parser.read_document`: ``metadata`` holds ``{kind,
category: "documentation", frontmatter, title, headings}`` (the ESM lines are not outline), and,
under statement capture, the whole file is one ``doc`` / ``readme`` / ``changelog`` / …
statement, secrets masked — as an ``.md`` file would give. The File itself stays ``type:
"code"``.

A parse that times out keeps the File record and logs ``mdx.parse_timeout`` once per stage:
after a ``block`` timeout the rest of the file is not probed and the document has no outline;
after a ``file`` timeout no import is read.

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
from ..markdown.parser import read_document
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
    text and the fence it opens is still seen. A parse timeout raises ``ValueError``."""
    root = parse_source(_GRAMMAR, code, timeout).root_node
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
        timed_out: set[str] = set()

        def parse_timed_out(stage: str) -> None:
            # Fail loudly, once per stage: a ``block`` timeout leaves that block and the ones
            # after it unread; a ``file`` timeout leaves every import unknown.
            if stage not in timed_out:
                timed_out.add(stage)
                log.warning("mdx.parse_timeout", path=ctx.path, stage=stage)

        def parses(code: bytes) -> bool:
            if "block" in timed_out:
                return False  # one slow block is enough: the rest of the file is not probed
            try:
                return is_esm(code, timeout)
            except ValueError:  # parse timeout: a block we cannot read is not code
                parse_timed_out("block")
                return False

        def budget_spent() -> None:
            # The scan stopped reading ESM here: imports after this point may be missing.
            log.warning("mdx.esm_parse_budget_reached", path=ctx.path, budget=MAX_PARSES_PER_FILE)

        shadow = esm_shadow(ctx.source, parses, budget_spent)
        # The lines the scan found to be ESM are code, not Markdown: left out of the document's
        # outline even when they do not parse below. After a block timeout, which lines are
        # code is unknown, so the file has no outline rather than a guessed one.
        code_lines = (
            _all_lines(ctx.source) if "block" in timed_out else _code_lines(ctx.source, shadow)
        )
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
            parse_timed_out("file")
            shadow = b""
            root = parse_source(_GRAMMAR, shadow).root_node

        internal, external, exports, _ = extract_imports(
            root, shadow, ctx.path, ctx.repo_root, ctx.related_indexes.get("typescript")
        )
        # The prose is a Markdown document, read as the Markdown parser reads one.
        doc = read_document(ctx, code_lines=code_lines)
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
            metadata=doc.metadata,
        )
        if doc.statement is not None:
            # Before the detectors run: the document keeps the id the Markdown parser gave it,
            # and a page record at the same spot is disambiguated after it.
            record.statements.append(doc.statement)
        run_additive("mdx", root, ctx, record, is_fixture=self.is_fixture_file(ctx.path))
        return record


def _code_lines(source: bytes, shadow: bytes) -> set[int]:
    """The 1-based lines of the document text (:func:`read_document`, where a lone ``\\r`` ends
    a line too) that hold ESM code in ``shadow``. The shadow keeps the file's bytes and
    newlines, so its lines pair with the source's; a lone ``\\r`` inside a source line adds a
    document line from there on."""
    out: set[int] = set()
    extra = 0  # document lines added so far by lone ``\\r``
    for i, (src, code) in enumerate(zip(source.split(b"\n"), shadow.split(b"\n"), strict=True)):
        lone = src.count(b"\r") - src.endswith(b"\r")
        if code.strip():
            out.update(range(i + extra + 1, i + extra + lone + 2))
        extra += lone
    return out


def _all_lines(source: bytes) -> set[int]:
    """Every line of the document text (an upper bound: ``\\n`` and ``\\r`` each counted)."""
    return set(range(1, source.count(b"\n") + source.count(b"\r") + 2))
