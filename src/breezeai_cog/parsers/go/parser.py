"""GoParser — extracts one .go file or a go.mod/go.sum config file into a FileRecord."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from tree_sitter import Node

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
    build_anonymous_function,
    build_function,
    scan_file,
)
from .declarations import Declarations, declarations, package_dir
from .imports import GoIndex, build_fqcn_index, extract_imports
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
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.statement_text_limit

        idx = ctx.resolution_index
        external, packages = extract_imports(root, source, idx if isinstance(idx, GoIndex) else None)
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
                    resolve=resolve,
                )
                functions.append(fn)
                owner_ids[child.start_byte] = fn.id
                statements.extend(fn_statements)
                if fn.name[:1].isupper():
                    exports.add(fn.name)
                if owner is not None:
                    owner.metadata = {**(owner.metadata or {}), "hasMethods": True}
                if fn.type == "constructor":
                    target_name = _constructor_target_name(fn.name, fn.returnType)
                    target = class_by_name.get(target_name)
                    if target is not None:
                        target.constructorParams = [ConstructorParam(name=p.name, type=p.type) for p in fn.params]

        for node, owner in scan.literals:  # document order: an enclosing literal comes first
            fn = build_anonymous_function(
                node, source, path,
                parent_id=owner_ids.get(owner, fid) if owner is not None else fid,
                seen_ids=seen_ids, resolve=resolve,
            )
            functions.append(fn)
            owner_ids[node.start_byte] = fn.id

        # Package-level declarations (`const BaseURL = "/api"`, `var db *gorm.DB`). The walk
        # stops at NESTED_SCOPES, so function bodies are not re-walked. Runs before the comment
        # pass so a same-line trailing comment rides on the statement's `text`.
        statements.extend(
            extract_statements(root, source, path, parent_id=fid, capture=capture,
                               limit=limit, seen_ids=seen_ids)
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
