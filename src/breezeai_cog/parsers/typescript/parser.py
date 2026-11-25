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
from ..additive import run_additive
from ..base import BaseParser, HandlerLocator, ParseContext
from ..treesitter import node_text, parse_source
from .classes import build_class
from ..callresolve import make_resolver
from .decorators import extract_decorators
from .functions import (
    _calls as body_calls,
    track_built,
    build_function,
    collect_nested_functions,
    defined_names,
    type_map,
)
from ..statements_common import reset_http_client_ids, set_http_client_ids
from .call_targets import checked_resolver, follow_reexports, module_objects
from .declarations import import_bindings
from .handlers import handler_locator
from .imports import TsAliasIndex, build_ts_index, extract_imports
from ..comments_common import comment_statements_for
from .mappings import (
    COMMENT_TYPES,
    CONTROL_FLOW,
    FRAMEWORKS,
    JS_ARRAY_TYPES,
    JS_BUILTIN_METHODS,
    STATEMENT_TYPES,
)
from .statements import collect_http_client_ids, extract_statements

_DECLS = (
    "class_declaration",
    "abstract_class_declaration",
    "interface_declaration",
    "enum_declaration",
    "function_declaration",
    "lexical_declaration",
    "variable_declaration",
)
_CLASSES = (
    "class_declaration",
    "abstract_class_declaration",
    "interface_declaration",
    "enum_declaration",
)
_TSX_EXT = (".tsx", ".jsx")
_JS_EXT = (".js", ".jsx", ".mjs", ".cjs")
#: JS/TS route-only fixture markers, layered on top of the global set (base.py). Storybook
#: stories (not covered by the universal test-file ignores) + Cypress/Playwright specs.
_TS_FIXTURE_MARKERS = (".stories.", ".cy.", ".e2e.", ".mock.")


_FUNC_VALUES = ("arrow_function", "function_expression")
#: Class nodes: their code is the class's (its methods, field initializers), never the file's.
_CLASS_NODES = frozenset({"class_declaration", "class", "abstract_class_declaration"})
#: What the file-level pass does not enter: classes, namespaces (walked on their own) and
#: decorators (a decorator is not a call the file makes).
_FILE_OPAQUE = _CLASS_NODES | frozenset({"internal_module", "module", "decorator"})
#: Bare calls not recorded in ``File.calls``: ``require(…)`` is an import (``importFiles``).
_FILE_SKIP_CALLS = frozenset({"require"})
#: `namespace X {…}` and the legacy `module X {…}` — a declaration scope, not a value.
_MODULE_TYPES = ("internal_module", "module")


def _unwrap_export(node: Node) -> tuple[Node | None, list[Node]]:
    if node.type in ("export_statement", "ambient_declaration"):
        # `ambient_declaration` is the `declare …` wrapper: `declare const enum E {…}` holds
        # its enum_declaration directly. A body-less `declare function f(): void` is a
        # `function_signature` — not in _DECLS, so it is skipped rather than turned into a
        # Function with no body.
        decs = [c for c in node.named_children if c.type == "decorator"]
        decl = next((c for c in node.named_children if c.type in _DECLS), None)
        return decl, decs
    if node.type in _DECLS:
        return node, []
    return None, []


def _module_block(node: Node) -> Node | None:
    """The ``statement_block`` body of a namespace/module wrapper, else ``None``.

    TypeScript spells this five ways, none of them a plain declaration: ``namespace X {…}``
    (wrapped in an ``expression_statement``), ``export namespace X {…}`` (in an
    ``export_statement``), ``module X {…}`` (a bare ``module`` node), ``declare
    namespace/module X {…}`` (in an ``ambient_declaration``) and ``declare global {…}``
    (an ``ambient_declaration`` straight to the block). Members inside are extracted as
    if they were top-level — the model's only class containment edge is File→Class, so a
    namespace is flattened exactly like a C++ one."""
    if node.type in ("expression_statement", "export_statement", "ambient_declaration"):
        inner = next(
            (c for c in node.named_children if c.type in _MODULE_TYPES or c.type == "statement_block"),
            None,
        )
        return _module_block(inner) if inner is not None else None
    if node.type in _MODULE_TYPES:
        return next((c for c in node.named_children if c.type == "statement_block"), None)
    if node.type == "statement_block":  # `declare global { … }`
        return node
    return None


#: Where a module stores what it exports: `exports.x`, `module.exports.x`, `module.exports`.
_EXPORTS = ("exports", "module.exports")


def _assignment(node: Node) -> Node | None:
    """The ``assignment_expression`` a statement is made of, else None."""
    if node.type != "expression_statement":
        return None
    inner = [c for c in node.named_children if c.type != "comment"]
    return inner[0] if len(inner) == 1 and inner[0].type == "assignment_expression" else None


def _assignment_counts(root: Node, source: bytes) -> dict[str, int]:
    counts: dict[str, int] = {}
    for child in root.named_children:
        a = _assignment(child)
        left = a.child_by_field_name("left") if a is not None else None
        if left is not None:
            key = "".join(node_text(left, source).split())
            counts[key] = counts.get(key, 0) + 1
    return counts


def _assigned_function(
    node: Node, source: bytes, counts: dict[str, int],
) -> tuple[Node, str, str | None] | None:
    """A function stored where callers find it by name (Rule 2a): (value, name, receiverType).
    ``exports.x = …`` / ``module.exports.x = …`` → ``x``; ``module.exports = …`` →
    ``default`` (what ``require`` returns, as an ES default export); ``X.prototype.m = …`` →
    ``m`` with receiver ``X``. Never an ambiguous name: a chain (``exports.a = exports.b =
    …``) or a target assigned twice in the file gives None."""
    a = _assignment(node)
    if a is None:
        return None
    left, right = a.child_by_field_name("left"), a.child_by_field_name("right")
    if left is None or right is None or right.type not in _FUNC_VALUES + ("function",):
        return None
    target = "".join(node_text(left, source).split())
    if counts.get(target, 0) != 1:
        return None
    if target == "module.exports":
        return right, "default", None
    owner, _, member = target.rpartition(".")
    if not member.isidentifier():
        return None
    if owner in _EXPORTS:
        return right, member, None
    base, _, proto = owner.rpartition(".")
    if proto == "prototype" and base.isidentifier():
        return right, member, base
    return None


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
    extensions: tuple[str, ...] = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def fixture_markers(self) -> tuple[str, ...]:
        # Global set + JS/TS additions (``_TS_FIXTURE_MARKERS``); inherited by every TS
        # framework parser (React/NestJS/Express/…).
        return (*super().fixture_markers(), *_TS_FIXTURE_MARKERS)

    def build_index(
        self, repo_root: Path, files: Sequence[Path], jobs: int = 1
    ) -> TsAliasIndex | None:
        """Repo-level pre-pass: tsconfig path aliases + a string-constant value map (for
        resolving non-literal route paths like Angular's ``path: RouteNames.X``)."""
        return build_ts_index(Path(repo_root), files, jobs)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        grammar = "tsx" if ctx.path.endswith(_TSX_EXT) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def handler_locator(self, ctx: ParseContext) -> HandlerLocator | None:
        # Asked for only when a record names a handler without a line — the rare case, so the
        # extra parse is rare too.
        grammar = "tsx" if ctx.path.endswith(_TSX_EXT) else "typescript"
        root = parse_source(grammar, ctx.source, ctx.parse_timeout_micros).root_node
        return handler_locator(root, ctx.source, ctx.path, ctx.repo_root, ctx.resolution_index)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        # Collect the per-file wrapped-HTTP-client names and make them available to the shared
        # statement classifier for the duration of this file's extraction, resetting in a
        # finally so the value never leaks into the next file a reused worker handles.
        ids = collect_http_client_ids(root, ctx.source) if ctx.capture_statements else frozenset()
        token = set_http_client_ids(ids)
        try:
            return self._extract(root, ctx)
        finally:
            reset_http_client_ids(token)

    def _extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.statement_text_limit

        internal, external, exports, bindings = extract_imports(
            root, source, path, ctx.repo_root, ctx.resolution_index
        )
        # calls[].path: an import followed through re-exports to its declaring file, and a path
        # kept only when that file declares what is called (call_targets.py)
        imported = import_bindings(root, source)
        called = follow_reexports(bindings, imported, ctx.resolution_index)
        types = type_map(root, source)
        resolve = checked_resolver(
            make_resolver(  # Tiers 1+2 + Phase 2 + inherited this.M()
                called,
                defined_names(root, source),
                path,
                types,
                heritage=getattr(ctx.resolution_index, "class_heritage", None),
                builtin_methods=JS_BUILTIN_METHODS,
                array_types=JS_ARRAY_TYPES,
            ),
            path=path, bindings=called, types=types, index=ctx.resolution_index,
            namespaces=module_objects(root, source, imported),
        )
        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []
        #: Spans of the declaration values extracted as Functions (`const f = () => …`): a
        #: statement that only binds them is not emitted.
        built: set[tuple[int, int]] = set()
        #: Statements of the Functions found inside file-level code (`setInterval(function
        #: tick() {…})`): listed after the file's own, as a function's nested ones follow its
        #: own, so a detector enriching "the statement on this line" finds the outer call.
        nested_statements: list[Statement] = []

        def walk(scope: Node) -> None:
            """Dispatch every declaration in ``scope``, descending into namespace/module
            bodies so their members are extracted as if they were top-level (they parent to
            the file — see :func:`_module_block`). Namespaces nest, hence the recursion."""
            pending: list[Node] = []
            for child in scope.named_children:
                if child.type == "decorator":
                    pending.append(child)
                    continue
                if child.type == "comment":
                    continue  # keep pending decorators across a comment before the declaration
                block = _module_block(child)
                if block is not None:
                    pending = []
                    walk(block)
                    # The namespace body is a barrier for the file-root statement pass (see
                    # NESTED_SCOPES), so collect its own declarations here — they parent to
                    # the file, like the classes/functions `walk` just dispatched.
                    statements.extend(
                        extract_statements(
                            block, source, path, parent_id=fid, capture=capture,
                            limit=limit, seen_ids=seen_ids, built=frozenset(built),
                        )
                    )
                    continue
                decl, exp_decs = _unwrap_export(child)
                decorators = pending + exp_decs
                pending = []
                assigned = _assigned_function(child, source, assignments)
                if assigned is not None:
                    # `exports.load = function loadAll() {…}`: a Function named where it is
                    # stored (Rule 2a); its own name, if any, is its alias.
                    value, fn_name, receiver = assigned
                    built.add((value.start_byte, value.end_byte))
                    fns, fn_stmts = build_function(
                        value, name=fn_name, kind=value.type, decorators=[], source=source,
                        path=path, parent_id=fid, class_name=None, seen_ids=seen_ids,
                        capture=capture, limit=limit, resolve=resolve,
                    )
                    if receiver is not None and fns:
                        fns[0].receiverType = receiver
                    functions.extend(fns)
                    nested_statements.extend(fn_stmts)
                    continue
                if decl is None and child.type != "export_statement":
                    # Code at file level (`setInterval(function tick() {…})`, `$('#b').click(
                    # function () {…})`): a function with a name of its own, or bound to a name
                    # inside a nameless callback, is a Function of the file; the rest of the
                    # code is the file's own (File.calls and statements, after the walk).
                    for value_node, nested_name, nested_kind in collect_nested_functions(
                        child, source
                    ):
                        fns, fn_stmts = build_function(
                            value_node, name=nested_name, kind=nested_kind, decorators=[],
                            source=source, path=path, parent_id=fid, class_name=None,
                            seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
                        )
                        functions.extend(fns)
                        nested_statements.extend(fn_stmts)
                    continue
                if decl is None:
                    # `export default <expr>` is not a declaration, so it isn't handled above —
                    # but it commonly wraps a named function: `export default React.memo(function
                    # Foo(){…})`, `export default function bar(){}`. Descend it with the named-only
                    # collector so the component (and, via build_function's recursion, its nested
                    # handlers) are seeded. Anonymous `export default () => …` yields nothing.
                    if child.type == "export_statement" and self._export_default(
                        child, decorators, source, path, fid, seen_ids, capture, limit,
                        functions, classes, statements, resolve,
                    ):
                        continue
                    if child.type == "export_statement":
                        for value_node, nested_name, nested_kind in collect_nested_functions(
                            child, source
                        ):
                            fns, fn_stmts = build_function(
                                value_node,
                                name=nested_name or "default",
                                kind=nested_kind,
                                decorators=[],
                                source=source,
                                path=path,
                                parent_id=fid,
                                class_name=None,
                                seen_ids=seen_ids,
                                capture=capture,
                                limit=limit,
                                resolve=resolve,
                            )
                            functions.extend(fns)
                            statements.extend(fn_stmts)
                    continue
                self._handle(
                    decl,
                    decorators,
                    source,
                    path,
                    fid,
                    seen_ids,
                    capture,
                    limit,
                    functions,
                    classes,
                    statements,
                    resolve,
                    built,
                )

        # How many times each target is assigned at file level: one assigned twice
        # (`exports.x = …` then `exports.x = …`) names no Function (Rule 2e).
        assignments = _assignment_counts(root, source)
        with track_built() as built_spans:
            walk(root)

        # The file's own code: everything outside a Function or a Class, nameless callbacks
        # included (`process.on('SIGTERM', () => { … })`) — its statements and its calls.
        owned = frozenset(built_spans)
        statements.extend(
            extract_statements(
                root, source, path, parent_id=fid, capture=capture, limit=limit,
                seen_ids=seen_ids, built=frozenset(built), descend_all=True,
                barriers=owned, opaque=_FILE_OPAQUE,
            )
        )
        statements.extend(nested_statements)
        file_calls = body_calls(root, source, resolve, None, owned,
                                opaque=_FILE_OPAQUE, skip=_FILE_SKIP_CALLS)

        if capture:
            statements.extend(
                comment_statements_for(
                    root, source, path, file_id=fid, functions=functions, classes=classes,
                    statements=statements, control_flow=CONTROL_FLOW,
                    comment_types=COMMENT_TYPES, limit=limit, seen_ids=seen_ids,
                )
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
            calls=file_calls or None,
        )
        # Additive detectors (parsers/additive.py) — layered on top of base + framework
        # extraction for every TS parser that inherits extract, so they also fire in files owned
        # by another framework. A more-specific framework label set by a subclass afterwards wins.
        run_additive(
            "typescript", root, ctx, record,
            is_fixture=self.is_fixture_file(path), bindings=bindings,
        )
        return record

    def _handle(
        self,
        decl,
        decorators,
        source,
        path,
        fid,
        seen_ids,
        capture,
        limit,
        functions,
        classes,
        statements,
        resolve,
        built,
    ) -> None:
        if decl.type in _CLASSES:
            cls, methods, cls_stmts = build_class(
                decl,
                decorators,
                source,
                path,
                parent_id=fid,
                seen_ids=seen_ids,
                capture=capture,
                limit=limit,
                resolve=resolve,
            )
            classes.append(cls)
            functions.extend(methods)
            statements.extend(cls_stmts)
        elif decl.type == "function_declaration":
            name_node = decl.child_by_field_name("name")
            fns, fn_stmts = build_function(
                decl,
                name=node_text(name_node, source) if name_node else "",
                kind="function",
                decorators=extract_decorators(decorators, source),
                source=source,
                path=path,
                parent_id=fid,
                class_name=None,
                seen_ids=seen_ids,
                capture=capture,
                limit=limit,
                resolve=resolve,
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
                    built.add((value.start_byte, value.end_byte))
                    fns, fn_stmts = build_function(
                        value,
                        name=decl_name,
                        kind=value.type,
                        decorators=[],
                        source=source,
                        path=path,
                        parent_id=fid,
                        class_name=None,
                        seen_ids=seen_ids,
                        capture=capture,
                        limit=limit,
                        resolve=resolve,
                    )
                    functions.extend(fns)
                    statements.extend(fn_stmts)
                elif value is not None and value.type == "object" and _bears_function(value):
                    # G2: functions attached as object-literal properties (resolver maps,
                    # service/hook objects). Descend from this declaration site, recursing
                    # only through function-bearing sub-objects (Query/Mutation groupings).
                    self._object_functions(
                        value,
                        decl_name,
                        source,
                        path,
                        fid,
                        seen_ids,
                        capture,
                        limit,
                        functions,
                        statements,
                        resolve,
                    )
                elif value is not None:
                    # Value is a wrapper expression (call/parens/ternary/array/await/as …)
                    # that may hide named functions — e.g. Zustand `create((set) => ({ open:
                    # () => … }))`, nested `create(persist(…))`, Sequelize `hasMany(() => M)`.
                    # Reuse the named-only nested collector: it descends call args at any
                    # depth and seeds by property key / binding name, skipping anonymous
                    # callbacks (the same machinery build_class uses for decorator args). This
                    # is callee-agnostic — no wrapper-name allow-list.
                    for value_node, nested_name, nested_kind in collect_nested_functions(
                        value, source
                    ):
                        fns, fn_stmts = build_function(
                            value_node,
                            name=nested_name or decl_name,
                            kind=nested_kind,
                            decorators=[],
                            source=source,
                            path=path,
                            parent_id=fid,
                            class_name=None,
                            seen_ids=seen_ids,
                            capture=capture,
                            limit=limit,
                            resolve=resolve,
                        )
                        functions.extend(fns)
                        statements.extend(fn_stmts)

    def _export_default(
        self, export, decorators, source, path, fid, seen_ids, capture, limit, functions,
        classes, statements, resolve,
    ) -> bool:
        """``export default () => …`` / ``export default function () {…}``: a Function named
        ``default`` — what an importer of the default export calls; ``export default class {…}``:
        a Class ``default``, ``isAnonymous``. True when handled; a named value, an object or
        any other expression is left to the caller."""
        if not any(c.type == "default" for c in export.children):
            return False
        value = next((c for c in export.named_children
                      if c.type not in ("decorator", "comment")), None)
        if value is None:
            return False
        if value.type in _FUNC_VALUES and value.child_by_field_name("name") is None:
            fns, fn_stmts = build_function(
                value, name="default", kind=value.type, decorators=[], source=source,
                path=path, parent_id=fid, class_name=None, seen_ids=seen_ids, capture=capture,
                limit=limit, resolve=resolve,
            )
            functions.extend(fns)
            statements.extend(fn_stmts)
            return True
        if value.type == "class" and value.child_by_field_name("name") is None:
            cls, methods, cls_stmts = build_class(
                value, decorators, source, path, parent_id=fid, seen_ids=seen_ids,
                capture=capture, limit=limit, resolve=resolve, default_name="default",
            )
            classes.append(cls)
            functions.extend(methods)
            statements.extend(cls_stmts)
            return True
        return False

    def _object_functions(
        self,
        obj,
        name_prefix,
        source,
        path,
        fid,
        seen_ids,
        capture,
        limit,
        functions,
        statements,
        resolve,
    ) -> None:
        for pair in obj.named_children:
            if pair.type == "method_definition":
                # Object shorthand method: `{ n() {} }` (name from the property key).
                key = pair.child_by_field_name("name")
                if key is None:
                    continue
                fns, fn_stmts = build_function(
                    pair,
                    name=f"{name_prefix}.{_member_name(key, source)}",
                    kind="method",
                    decorators=[],
                    source=source,
                    path=path,
                    parent_id=fid,
                    class_name=None,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resolve=resolve,
                )
                functions.extend(fns)
                statements.extend(fn_stmts)
                continue
            if pair.type != "pair":
                continue
            key = pair.child_by_field_name("key")
            value = pair.child_by_field_name("value")
            if key is None or value is None:
                continue
            name = f"{name_prefix}.{_member_name(key, source)}"
            if value.type in _FUNC_VALUES:
                fns, fn_stmts = build_function(
                    value,
                    name=name,
                    kind=value.type,
                    decorators=[],
                    source=source,
                    path=path,
                    parent_id=fid,
                    class_name=None,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resolve=resolve,
                )
                functions.extend(fns)
                statements.extend(fn_stmts)
            elif value.type == "object" and _bears_function(value):
                self._object_functions(
                    value,
                    name,
                    source,
                    path,
                    fid,
                    seen_ids,
                    capture,
                    limit,
                    functions,
                    statements,
                    resolve,
                )
