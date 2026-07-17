"""TypeScriptParser — extracts one .ts/.tsx/.js/.jsx file into a FileRecord.

Picks the ``tsx`` grammar for JSX files and ``typescript`` otherwise; labels the
record language ``typescript`` or ``javascript`` by extension. Top-level
declarations may be wrapped in ``export_statement`` and preceded by ``decorator``
nodes, both of which are unwrapped here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Function, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..treesitter import node_text, parse_source
from .classes import build_class
from ..callresolve import make_resolver
from .aws_events import detect_aws_events
from ..typescript_express.routes import detect_express
from .functions import build_function, defined_names, extract_decorators, type_map
from .imports import TsAliasIndex, build_ts_index, extract_imports
from .mappings import FRAMEWORKS, STATEMENT_TYPES
from .statements import extract_statements

_DECLS = (
    "class_declaration", "abstract_class_declaration", "interface_declaration",
    "enum_declaration", "function_declaration", "lexical_declaration", "variable_declaration",
)
_CLASSES = ("class_declaration", "abstract_class_declaration", "interface_declaration", "enum_declaration")
_TSX_EXT = (".tsx", ".jsx")
_JS_EXT = (".js", ".jsx", ".mjs", ".cjs")
#: JS/TS route-only fixture markers, layered on top of the global set (base.py). Storybook
#: stories (not covered by the universal test-file ignores) + Cypress/Playwright specs.
_TS_FIXTURE_MARKERS = (".stories.", ".cy.", ".e2e.")


_FUNC_VALUES = ("arrow_function", "function_expression")


def _unwrap_export(node: Node) -> tuple[Node | None, list[Node]]:
    if node.type == "export_statement":
        decs = [c for c in node.named_children if c.type == "decorator"]
        decl = next((c for c in node.named_children if c.type in _DECLS), None)
        return decl, decs
    if node.type in _DECLS:
        return node, []
    return None, []


def _bears_function(obj: Node) -> bool:
    """Whether an object literal *transitively* contains a function-valued property.
    The guard for descending a sub-object (G2): we recurse into a nested object only if
    it actually holds functions, so plain data objects are never walked."""
    for pair in obj.named_children:
        if pair.type != "pair":
            continue
        value = pair.child_by_field_name("value")
        if value is None:
            continue
        if value.type in _FUNC_VALUES:
            return True
        if value.type == "object" and _bears_function(value):
            return True
    return False


def _member_name(key: Node, source: bytes) -> str:
    text = node_text(key, source)
    return text.strip("'\"`") if key.type == "string" else text


class TypeScriptParser(BaseParser):
    name = "typescript"
    extensions = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def fixture_markers(self) -> tuple[str, ...]:
        # Global set + JS/TS additions (``_TS_FIXTURE_MARKERS``); inherited by every TS
        # framework parser (React/NestJS/Express/…).
        return (*super().fixture_markers(), *_TS_FIXTURE_MARKERS)

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> TsAliasIndex | None:
        """Repo-level pre-pass: tsconfig path aliases + a string-constant value map (for
        resolving non-literal route paths like Angular's ``path: RouteNames.X``)."""
        return build_ts_index(Path(repo_root), files, jobs)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        grammar = "tsx" if ctx.path.endswith(_TSX_EXT) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.text_truncation_limit

        internal, external, exports, bindings = extract_imports(
            root, source, path, ctx.repo_root, ctx.resolution_index
        )
        resolve = make_resolver(  # calls[].path (Tiers 1+2 + Phase 2 + inherited this.M())
            bindings, defined_names(root, source), path, type_map(root, source),
            heritage=getattr(ctx.resolution_index, "class_heritage", None),
        )
        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []

        pending: list[Node] = []
        for child in root.named_children:
            if child.type == "decorator":
                pending.append(child)
                continue
            if child.type == "comment":
                continue  # keep pending decorators across a comment before the declaration
            decl, exp_decs = _unwrap_export(child)
            decorators = pending + exp_decs
            pending = []
            if decl is None:
                continue
            self._handle(decl, decorators, source, path, fid, seen_ids, capture, limit,
                         functions, classes, statements, resolve)

        statements.extend(
            extract_statements(root, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids)
        )

        record = FileRecord(
            id=fid,
            path=path,
            type="code",
            language="javascript" if path.endswith(_JS_EXT) else "typescript",
            loc=count_loc(source.decode("utf-8", "replace")),
            importFiles=internal,
            externalImports=external,
            exports=exports,
            functions=functions,
            classes=classes,
            statements=statements,
        )
        # Additive route/event detection — gated by --capture-statements and layered on top
        # of base + framework extraction (runs for every TS parser that inherits extract, so
        # it also fires in files owned by another framework). Each detector self-guards on a
        # cheap marker. A more-specific framework label set by a subclass afterwards wins.
        if capture:
            if not self.is_fixture_file(path) and detect_express(root, source, path, record):
                if record.framework is None:
                    record.framework = "express"
            aws_fw = detect_aws_events(root, source, path, record)
            if aws_fw and record.framework is None:
                record.framework = aws_fw
        return record

    def _handle(self, decl, decorators, source, path, fid, seen_ids, capture, limit,
                functions, classes, statements, resolve) -> None:
        if decl.type in _CLASSES:
            cls, methods, cls_stmts = build_class(
                decl, decorators, source, path,
                parent_id=fid, seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
            )
            classes.append(cls)
            functions.extend(methods)
            statements.extend(cls_stmts)
        elif decl.type == "function_declaration":
            name_node = decl.child_by_field_name("name")
            fns, fn_stmts = build_function(
                decl, name=node_text(name_node, source) if name_node else "",
                kind="function", decorators=extract_decorators(decorators, source),
                source=source, path=path, parent_id=fid, class_name=None,
                seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
            )
            functions.extend(fns)
            statements.extend(fn_stmts)
        elif decl.type in ("lexical_declaration", "variable_declaration"):
            for vd in decl.named_children:
                if vd.type != "variable_declarator":
                    continue
                value = vd.child_by_field_name("value")
                name_node = vd.child_by_field_name("name")
                decl_name = node_text(name_node, source) if name_node else ""
                if value is not None and value.type in _FUNC_VALUES:
                    fns, fn_stmts = build_function(
                        value, name=decl_name,
                        kind=value.type, decorators=[], source=source, path=path,
                        parent_id=fid, class_name=None, seen_ids=seen_ids,
                        capture=capture, limit=limit, resolve=resolve,
                    )
                    functions.extend(fns)
                    statements.extend(fn_stmts)
                elif value is not None and value.type == "object" and _bears_function(value):
                    # G2: functions attached as object-literal properties (resolver maps,
                    # service/hook objects). Descend from this declaration site, recursing
                    # only through function-bearing sub-objects (Query/Mutation groupings).
                    self._object_functions(
                        value, decl_name, source, path, fid, seen_ids, capture, limit,
                        functions, statements, resolve,
                    )

    def _object_functions(self, obj, name_prefix, source, path, fid, seen_ids, capture,
                          limit, functions, statements, resolve) -> None:
        for pair in obj.named_children:
            if pair.type != "pair":
                continue
            key = pair.child_by_field_name("key")
            value = pair.child_by_field_name("value")
            if key is None or value is None:
                continue
            name = f"{name_prefix}.{_member_name(key, source)}"
            if value.type in _FUNC_VALUES:
                fns, fn_stmts = build_function(
                    value, name=name, kind=value.type, decorators=[], source=source,
                    path=path, parent_id=fid, class_name=None, seen_ids=seen_ids,
                    capture=capture, limit=limit, resolve=resolve,
                )
                functions.extend(fns)
                statements.extend(fn_stmts)
            elif value.type == "object" and _bears_function(value):
                self._object_functions(
                    value, name, source, path, fid, seen_ids, capture, limit,
                    functions, statements, resolve,
                )
