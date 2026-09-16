"""PhpParser — extracts one .php / .phtml file into a FileRecord."""

from __future__ import annotations

import json
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ...emit import SeenIds, file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Function, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..callresolve import make_resolver
from ..comments_common import comment_statements_for
from ..treesitter import node_text, parse_source
from .classes import build_class
from .functions import build_function, collect_closures, defined_names, type_map
from .imports import PhpIndex, build_php_index, extract_imports
from .mappings import COMMENT_TYPES, CONTROL_FLOW, FRAMEWORKS, STATEMENT_TYPES
from .statements import extract_statements
from .wordpress import detect_wordpress_hooks

_CLASS_TYPES = (
    "class_declaration",
    "interface_declaration",
    "trait_declaration",
    "enum_declaration",
)


@lru_cache(maxsize=128)
def _read_composer_requirements(composer_path: Path) -> frozenset[str] | None:
    try:
        if not composer_path.is_file():
            return None
        data = json.loads(composer_path.read_bytes())
        reqs = set()
        if isinstance(data, dict):
            for section in ("require", "require-dev"):
                sec = data.get(section)
                if isinstance(sec, dict):
                    reqs.update(sec.keys())
        return frozenset(reqs)
    except Exception:
        try:
            content = composer_path.read_bytes()
            return frozenset({pkg.decode("utf-8", errors="ignore") for pkg in (
                b"laravel/framework", b"symfony/framework-bundle", b"slim/slim", b"codeigniter4/framework"
            ) if pkg in content})
        except Exception:
            return None


def composer_requires(path: str | Path, package: bytes | str) -> bool:
    """Walk up parent directories from ``path`` looking for a composer.json requiring ``package``."""
    try:
        pkg_str = package.decode("utf-8") if isinstance(package, bytes) else package
        p = Path(path)
        paths_to_try = [p]
        if not p.is_absolute():
            paths_to_try.append(p.resolve())
        for pt in paths_to_try:
            start = pt.parent if pt.is_file() or pt.suffix else pt
            for d in [start, *start.parents]:
                reqs = _read_composer_requirements(d / "composer.json")
                if reqs is not None and pkg_str in reqs:
                    return True
    except Exception:
        pass
    return False


class PhpParser(BaseParser):
    name = "php"
    extensions = (".php", ".phtml")
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> PhpIndex:
        """Repo-level pre-pass: FQCN -> repo path map for PSR-4 import resolution."""
        return build_php_index(Path(repo_root), files, jobs)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext, seen_ids: Any | None = None) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        if seen_ids is None:
            seen_ids = SeenIds()
        capture, limit = ctx.capture_statements, ctx.statement_text_limit

        internal, external, exports, bindings = extract_imports(
            root, source, path, ctx.repo_root, ctx.resolution_index
        )
        resolve = make_resolver(bindings, defined_names(root, source), path, type_map(root, source))

        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []

        for child in root.named_children:
            if child.type in _CLASS_TYPES:
                cls_list, methods, cls_statements = build_class(
                    child,
                    decorators=[],
                    source=source,
                    path=path,
                    parent_id=fid,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resolve=resolve,
                )
                classes.extend(cls_list)
                functions.extend(methods)
                statements.extend(cls_statements)
            elif child.type == "function_definition":
                name_node = child.child_by_field_name("name")
                name = node_text(name_node, source) if name_node is not None else ""
                fns, fn_stmts = build_function(
                    child,
                    name=name,
                    kind="function",
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

        # Top-level closures have no named declaration to discover them.  Build a
        # Function node for each direct closure; nested closures are handled recursively.
        for closure in collect_closures(root):
            closure_kind = "arrow_function" if closure.type == "arrow_function" else "function_expression"
            fns, fn_stmts = build_function(
                closure,
                name="<anonymous>",
                kind=closure_kind,
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

        # File-root procedural statements (excluding extracted classes and functions)
        if capture:
            declared_spans = frozenset(
                (c.start_byte, c.end_byte)
                for c in root.named_children
                if c.type in _CLASS_TYPES or c.type == "function_definition"
            )
            closure_spans = frozenset(
                (closure.start_byte, closure.end_byte) for closure in collect_closures(root)
            )
            root_stmts = extract_statements(
                root,
                source,
                path,
                parent_id=fid,
                capture=capture,
                limit=limit,
                seen_ids=seen_ids,
                descend_all=False,
                barriers=declared_spans | closure_spans,
            )
            statements.extend(root_stmts)

            # WordPress hooks (additive detection)
            if not self.is_fixture_file(path) and (
                b"add_action" in source or b"add_filter" in source
            ):
                wp_hooks = detect_wordpress_hooks(
                    root, source, path, parent_id=fid, seen_ids=seen_ids
                )
                if wp_hooks:
                    statements.extend(wp_hooks)

            # Comments capture
            statements.extend(
                comment_statements_for(
                    root,
                    source,
                    path,
                    file_id=fid,
                    functions=functions,
                    classes=classes,
                    statements=statements,
                    control_flow=CONTROL_FLOW,
                    comment_types=COMMENT_TYPES,
                    limit=limit,
                    seen_ids=seen_ids,
                )
            )

        rec = FileRecord(
            id=fid,
            path=path,
            type="code",
            language="php",
            loc=count_loc(source.decode("utf-8", "replace")),
            importFiles=internal,
            externalImports=external,
            exports=exports,
            functions=functions,
            classes=classes,
            statements=statements,
        )
        rec._seen_ids = seen_ids
        return rec
