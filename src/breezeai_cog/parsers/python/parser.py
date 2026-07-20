"""PythonParser — orchestrates extraction of one ``.py`` file into a FileRecord."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Function, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..treesitter import parse_source
from ..callresolve import make_resolver
from .classes import build_class, iter_definitions
from .functions import build_function, defined_names, extract_decorators
from .imports import extract_imports
from .mappings import FRAMEWORKS, STATEMENT_TYPES
from .statements import extract_statements


class PythonParser(BaseParser):
    name = "python"
    extensions = (".py",)
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        """Parse the source into an AST, then extract. Split so subclasses /
        framework add-ons can reuse the same tree (no second parse) — see ``extract``."""
        root = parse_source("python", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        """Build a FileRecord from an already-parsed AST ``root``."""
        source = ctx.source
        path = ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.text_truncation_limit

        internal, external, exports, bindings = extract_imports(root, source, path, ctx.repo_root)
        resolve = make_resolver(bindings, defined_names(root, source), path)  # calls[].path

        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []
        # Walk the whole module (not just direct children): defs nested in
        # module-level blocks (``with DAG(...):``, ``if``/``for`` …) are seeded too.
        for defn, decs in iter_definitions(root):
            if defn.type == "class_definition":
                cls_list, methods, cls_statements = build_class(
                    defn, decs, source, path,
                    parent_id=fid, seen_ids=seen_ids, capture=capture, limit=limit, resolve=resolve,
                )
                classes.extend(cls_list)
                functions.extend(methods)
                statements.extend(cls_statements)
            else:  # function_definition
                fns, fn_statements = build_function(
                    defn, extract_decorators(decs, source), source, path,
                    parent_id=fid, class_name=None, seen_ids=seen_ids,
                    capture=capture, limit=limit, resolve=resolve,
                )
                functions.extend(fns)
                statements.extend(fn_statements)

        # file-scope statements (parented to the file)
        statements.extend(
            extract_statements(root, source, path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids)
        )

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="python",
            loc=count_loc(source.decode("utf-8", "replace")),
            importFiles=internal,
            externalImports=external,
            exports=exports,
            functions=functions,
            classes=classes,
            statements=statements,  # flat: file + class + function-scoped, linked by parentId
        )
