"""RubyParser — extracts one .rb file into a FileRecord."""

from __future__ import annotations

from collections.abc import Sequence
import re
import shlex
from pathlib import Path

from tree_sitter import Node

from ...emit import file_id
from ...schemas import SCHEMA_VERSION, FileRecord, Function, Statement
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..callresolve import CallResolver, make_resolver
from ..comments_common import comment_statements_for
from ..treesitter import parse_source
from .classes import build_class, iter_definitions
from .functions import _visibility_by_scope, build_function, defined_names
from .imports import extract_imports
from .mappings import COMMENT_TYPES, CONTROL_FLOW, FRAMEWORKS, STATEMENT_TYPES
from .models import (
    RubyModelIndex,
    build_model_index,
    declared_associations,
    declared_class_names,
    declared_parents,
    model_receiver_ids,
    model_receivers,
    resolve_model_names,
)
from .statements import extract_statements


_RUBY_FIXTURE_MARKERS: tuple[str, ...] = ("_spec.", "_test.")
_RUBY_FIXTURE_DIRS: frozenset[str] = frozenset({"spec", "test", "tests"})


class RubyParser(BaseParser):
    name = "ruby"
    extensions = (".rb", ".rake", ".gemspec", "Gemfile", "Rakefile")
    _RUBY_INTERPRETER = re.compile(r"(?:ruby(?:\d+(?:\.\d+)*)?|jruby|truffleruby|rbx)\Z")
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def fixture_markers(self) -> tuple[str, ...]:
        # RSpec / Minitest file naming (``cookies_spec.rb``, ``user_test.rb``): the file is a
        # test harness, so any router it builds is a throwaway, not an application route.
        return (*super().fixture_markers(), *_RUBY_FIXTURE_MARKERS)

    def is_fixture_file(self, path: str) -> bool:
        """Route-only fixture check: filename markers, global fixture dirs, plus the Ruby
        test-tree directories. Only route emission is suppressed; structure is still captured,
        and ``examples/``, ``lib/``, ``app/`` etc. are untouched."""
        if super().is_fixture_file(path):
            return True
        return any(
            seg in _RUBY_FIXTURE_DIRS for seg in path.replace("\\", "/").split("/")[:-1]
        )

    def matches_source(self, path: str | Path, source: bytes) -> bool:
        return self.matches(path) or (
            not Path(path).suffix and self._has_ruby_shebang(source.splitlines()[0] if source else b"")
        )

    @classmethod
    def _has_ruby_shebang(cls, first_line: bytes) -> bool:
        try:
            line = first_line.decode("utf-8").strip()
            if not line.startswith("#!"):
                return False
            words = shlex.split(line[2:])
        except (UnicodeDecodeError, ValueError):
            return False

        if not words:
            return False

        interpreter = Path(words[0]).name
        if interpreter == "env":
            args = words[1:]
            index = 0
            while index < len(args):
                arg = args[index]
                if arg in {"-S", "--split-string"}:
                    index += 1
                    if index >= len(args):
                        return False
                    try:
                        split_args = shlex.split(args[index])
                    except ValueError:
                        return False
                    args[index:index + 1] = split_args
                    continue
                if arg.startswith("--split-string="):
                    try:
                        split_args = shlex.split(arg.partition("=")[2])
                    except ValueError:
                        return False
                    args[index:index + 1] = split_args
                    continue
                if arg in {"-u", "--unset", "-C", "--chdir"}:
                    index += 2
                    continue
                if arg.startswith("--unset=") or arg.startswith("--chdir="):
                    index += 1
                    continue
                if arg in {"-i", "--ignore-environment", "-v", "--debug", "-"}:
                    index += 1
                    continue
                if arg.startswith("-"):
                    return False
                if "=" in arg:
                    index += 1
                    continue
                interpreter = Path(arg).name
                break
            else:
                return False

        return cls._RUBY_INTERPRETER.fullmatch(interpreter) is not None

    def claims(self, path: str, source: bytes, parse_timeout_micros: int = 0) -> bool:
        return True

    def build_index(
        self, repo_root: Path, files: Sequence[Path], jobs: int = 1
    ) -> RubyModelIndex:
        """Repo-level pre-pass: the ActiveRecord model constants (see ``models.py``).

        A model is declared in its own file but queried from controllers and services, so
        the identity that separates ``User.find`` from ``Tempfile.create`` can only be
        known repo-wide."""
        return build_model_index(Path(repo_root), files, jobs)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = ctx.parsed_root
        if root is None:
            root = parse_source("ruby", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:
        source, path = ctx.source, ctx.path
        fid = file_id(path)
        seen_ids: set[str] = set()
        capture, limit = ctx.capture_statements, ctx.statement_text_limit

        internal, external, exports, bindings = extract_imports(root, source, path, ctx.repo_root)

        def resolve_for_scope(scope: Node) -> CallResolver:
            return make_resolver(bindings, defined_names(scope, source), path)

        resolve = resolve_for_scope(root)

        index = ctx.resolution_index
        if isinstance(index, RubyModelIndex):
            models = set(index)
            associations = dict(index.associations)
        elif isinstance(index, frozenset):
            models = set(index)
            associations = {}
        else:
            models = set()
            associations = {}

        # Augment with models and associations declared directly in this file
        file_class_names = declared_class_names(root, source)
        file_parents = declared_parents(root, source)
        if file_parents:
            file_models = resolve_model_names(file_parents, file_class_names)
            models |= set(model_receiver_ids(file_models, file_class_names))
        file_assocs = declared_associations(root, source)
        for cls_name, assoc_name, kind, target_model in file_assocs:
            if cls_name in models or cls_name.rsplit("::", 1)[-1] in models:
                resolved_target: str | None = None
                if "::" in cls_name:
                    ns = cls_name.rsplit("::", 1)[0]
                    if f"{ns}::{target_model}" in models:
                        resolved_target = f"{ns}::{target_model}"
                if not resolved_target:
                    if target_model in models:
                        resolved_target = target_model
                    elif target_model.rsplit("::", 1)[-1] in models:
                        resolved_target = target_model.rsplit("::", 1)[-1]
                if resolved_target:
                    associations.setdefault(cls_name, {})[assoc_name] = (kind, resolved_target)
                    simple_cls = cls_name.rsplit("::", 1)[-1]
                    if simple_cls not in associations:
                        associations[simple_cls] = associations[cls_name]

        effective_models = frozenset(models)
        typed_db_ids = model_receivers(root, source, effective_models, associations) or None

        functions: list[Function] = []
        classes = []
        statements: list[Statement] = []

        visibility_scopes: dict[int, dict[tuple[int, int], str]] = {}
        for node in iter_definitions(root):
            if node.type in {"class", "module"}:
                cls_list, methods, cls_stmts = build_class(
                    node,
                    source,
                    path,
                    parent_id=fid,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resolve_for_scope=resolve_for_scope,
                    typed_db_ids=typed_db_ids,
                    models=effective_models,
                    associations=associations,
                )
                classes.extend(cls_list)
                functions.extend(methods)
                statements.extend(cls_stmts)
            elif node.type in {"method", "singleton_method"}:
                parent = node.parent
                visibility = None
                if parent is not None:
                    scope_id = id(parent)
                    if scope_id not in visibility_scopes:
                        visibility_scopes[scope_id] = _visibility_by_scope(parent, source)
                    visibility = visibility_scopes[scope_id].get((node.start_byte, node.end_byte))
                method_receivers = model_receivers(
                    node,
                    source,
                    effective_models,
                    associations,
                    enclosing_class=None,
                    is_instance_method=False,
                )
                fns, fn_stmts = build_function(
                    node,
                    source,
                    path,
                    parent_id=fid,
                    class_name=None,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resolve=resolve,
                    typed_db_ids=method_receivers,
                    visibility=visibility,
                )
                functions.extend(fns)
                statements.extend(fn_stmts)

        statements.extend(
            extract_statements(
                root, source, path, parent_id=fid, capture=capture, limit=limit,
                seen_ids=seen_ids, typed_db_ids=typed_db_ids,
            )
        )

        # Comments as flat Statements, scoped by the shared binding rule (see comments_common).
        if capture:
            statements.extend(
                comment_statements_for(
                    root, source, path, file_id=fid, functions=functions, classes=classes,
                    statements=statements, control_flow=CONTROL_FLOW,
                    comment_types=COMMENT_TYPES, limit=limit, seen_ids=seen_ids,
                )
            )

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="ruby",
            loc=count_loc(source.decode("utf-8", "replace")),
            importFiles=internal,
            externalImports=external,
            exports=exports,
            functions=functions,
            classes=classes,
            statements=statements,
        )
