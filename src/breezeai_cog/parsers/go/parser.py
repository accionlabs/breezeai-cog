"""GoParser — extracts one .go file or a go.mod/go.sum config file into a FileRecord."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from tree_sitter import Node

from ...errors import ParseError
from ...emit import file_id
from ...schemas import SCHEMA_VERSION, ConstructorParam, FileRecord, Function, Statement
from ...utils import count_loc
from ..additive import run_additive
from ..base import BaseParser, ParseContext
from ..callresolve import CallResolver, make_resolver
from ..comments_common import comment_statements_for
from ..treesitter import parse_source
from .classes import build_class
from .functions import (
    _constructor_target_name,
    build_literal_function,
    build_function,
    scan_file,
)
from .declarations import Declarations, declarations, package_dir
from .imports import GoIndex, build_fqcn_index, extract_imports, import_aliases
from .mappings import COMMENT_TYPES, CONTROL_FLOW, FRAMEWORKS, STATEMENT_TYPES
from .statements import extract_statements
from .gomod import parse_gomod
from .gosum import parse_gosum


def _resolver(packages: dict[str, str], names: set[str], types: dict[str, str], path: str,
              decls: Declarations) -> CallResolver:
    """Call → declaring file. ``pkg.Func()`` on an in-repo package and a same-package call to a
    function declared in another file resolve through the ``go.declarations`` fact to the file
    that declares the function (a package is a directory of files, so the import alone does not
    say which); everything else uses the shared resolver."""
    local = make_resolver({}, names, path, types)
    pkg = package_dir(path)

    def resolve(name: str, receiver: str | None, owner: str | None = None) -> str | None:
        if receiver in packages:
            site = decls.functions.get((packages[receiver], "", name))
            return site[0] if site else None
        if receiver is None and name not in names:
            site = decls.functions.get((pkg, "", name))
            return site[0] if site else None
        return local(name, receiver, owner)

    return resolve


def _owner(literal: Node, bound: frozenset[tuple[int, int]]) -> Node | None:
    """The nearest function, method or stored literal (``bound``) around ``literal``; None at
    package level. An inline literal on the way up is skipped: it is not a Function."""
    node = literal.parent
    while node is not None:
        if node.type in ("function_declaration", "method_declaration") or (
            node.type == "func_literal" and (node.start_byte, node.end_byte) in bound
        ):
            return node
        node = node.parent
    return None


def _first_syntax_error(root: Node) -> Node | None:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            return node
        stack.extend(
            reversed([
                child for child in node.children
                if child.has_error or child.is_missing
            ])
        )
    return None


class GoParser(BaseParser):
    name = "go"
    extensions = (".go", "go.mod", "go.sum")
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> GoIndex:
        return build_fqcn_index(Path(repo_root), files, jobs)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        path = Path(ctx.path)
        name = path.name
        source = ctx.source.decode("utf-8", "replace")

        if name == "go.mod":
            metadata = parse_gomod(path, source)
            return FileRecord(
                id=file_id(ctx.path),
                path=ctx.path,
                type="config",
                language="go",
                loc=count_loc(source),
                metadata=metadata,
            )

        if name == "go.sum":
            metadata = parse_gosum(path, source)
            return FileRecord(
                id=file_id(ctx.path),
                path=ctx.path,
                type="config",
                language="go",
                loc=count_loc(source),
                metadata=metadata,
            )

        root = parse_source("go", ctx.source, ctx.parse_timeout_micros).root_node
        if root.has_error:
            error = _first_syntax_error(root)
            if error is None:
                raise ParseError(
                    "Go syntax error reported by Tree-sitter",
                    path=ctx.path,
                    parser=self.name,
                )
            row, column = error.start_point
            detail = f"expected {error.type!r}" if error.is_missing else "unexpected syntax"
            raise ParseError(
                f"Go syntax error at line {row + 1}, column {column + 1}: {detail}",
                path=ctx.path,
                parser=self.name,
            )
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.statement_text_limit

        idx = ctx.resolution_index
        external, packages = extract_imports(root, source, idx if isinstance(idx, GoIndex) else None)
        resty_aliases = frozenset(
            alias for alias, module in import_aliases(root, source).items()
            if module in {"github.com/go-resty/resty/v2", "github.com/go-resty/resty/v3"}
        )
        scan = scan_file(root, source)
        decls = declarations(idx)
        resolve = _resolver(packages, scan.names, scan.types, path, decls)
        # importFiles: the files declaring the names this file uses from each in-repo package
        # (`users.Register` → users/routers.go), as C# resolves `using` namespaces — an import
        # names a directory, so no single file stands for it.
        internal = sorted({
            decls.symbols.get((packages[alias], name))
            for alias, name in scan.qualified if alias in packages
        } - {None, path})

        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []
        exports: set[str] = set()
        # If New and NewType both return a type, retain params from the first declaration.
        constructor_targets_seen: set[str] = set()

        class_by_name = {}
        for child in root.named_children:
            if child.type == "type_declaration":
                cls_list, cls_methods, cls_statements = build_class(
                    child, source, path, parent_id=fid, seen_ids=seen_ids,
                )
                classes.extend(cls_list)
                functions.extend(cls_methods)
                statements.extend(cls_statements)
                for cls in cls_list:
                    class_by_name[cls.name] = cls
                    if cls.name[:1].isupper():
                        exports.add(cls.name)

        # Function literals stored in a variable are Functions of their own (named after the
        # variable); every other literal is inline code of the function, or file, around it.
        bound = frozenset((node.start_byte, node.end_byte) for node, _ in scan.bound)
        owner_ids: dict[int, str] = {}  # function/literal start byte -> Function.id
        for child in root.named_children:
            if child.type in {"function_declaration", "method_declaration"}:
                receiver = child.child_by_field_name("receiver")
                receiver_name = None
                if receiver is not None:
                    text = receiver.text.decode("utf-8", "replace").strip("() ")
                    receiver_name = text.split()[-1].lstrip("*").rsplit(".", 1)[-1].split("[", 1)[0]
                owner = class_by_name.get(receiver_name)
                fn, fn_statements = build_function(
                    child, source, path,
                    parent_id=owner.id if owner is not None else fid,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resty_aliases=resty_aliases,
                    resolve=resolve,
                    barriers=bound,
                )
                functions.append(fn)
                owner_ids[child.start_byte] = fn.id
                statements.extend(fn_statements)
                if child.type == "function_declaration" and fn.name[:1].isupper():
                    exports.add(fn.name)
                if owner is not None:
                    owner.metadata = {**(owner.metadata or {}), "hasMethods": True}
                if fn.type == "constructor":
                    target_name = _constructor_target_name(fn.name, fn.returnType)
                    target = class_by_name.get(target_name)
                    if target is not None and target_name not in constructor_targets_seen:
                        target.constructorParams = [
                            ConstructorParam(name=p.name, type=p.type)
                            for p in fn.params
                            if p.type is not None
                        ]
                        constructor_targets_seen.add(target_name)

        for node, name in scan.bound:  # document order: an enclosing literal comes first
            enclosing = _owner(node, bound)
            fn, fn_statements = build_literal_function(
                node, name, source, path,
                parent_id=owner_ids.get(enclosing.start_byte, fid) if enclosing is not None else fid,
                package_level=enclosing is None,
                seen_ids=seen_ids, capture=capture, limit=limit,
                resty_aliases=resty_aliases,
                barriers=bound - {(node.start_byte, node.end_byte)}, resolve=resolve,
            )
            functions.append(fn)
            statements.extend(fn_statements)
            owner_ids[node.start_byte] = fn.id

        # Package-level declarations (`const BaseURL = "/api"`, `var db *gorm.DB`). The walk
        # stops at NESTED_SCOPES and stored literals, so function bodies are not re-walked; an
        # inline literal's statements belong to the file. Runs before the comment pass so a
        # same-line trailing comment rides on the statement's `text`.
        statements.extend(
            extract_statements(root, source, path, parent_id=fid, capture=capture,
                               limit=limit, seen_ids=seen_ids, resty_aliases=resty_aliases,
                               barriers=bound)
        )

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
            language="go",
            loc=count_loc(source.decode("utf-8", "replace")),
            importFiles=internal,
            externalImports=external,
            exports=sorted(exports),
            functions=functions,
            classes=classes,
            statements=statements,
        )
        # Additive detectors (parsers/additive.py): the Go router frameworks (go_*/) and anything
        # else registered for Go, layered on top of the base extraction.
        run_additive("go", root, ctx, record, is_fixture=self.is_fixture_file(path))
        return record
