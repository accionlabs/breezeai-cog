"""Twig template parser for PHP applications."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import SCHEMA_VERSION, FileRecord, Statement
from ...utils import count_loc, repo_relative
from ..base import BaseParser, ParseContext
from ..treesitter import line_span, node_text, parse_source

_CAPTURED_DIRECTIVES = {
    "assignment_statement",
    "for_statement",
    "from_statement",
    "if_statement",
    "import_statement",
    "macro_statement",
    "tag_statement",
}
_TEMPLATE_REFERENCE_TAGS = {"embed", "extends", "from", "import", "include", "use"}


@dataclass(frozen=True, slots=True)
class TwigTemplateIndex:
    paths: frozenset[str]

    def resolve(self, source_path: str, reference: str) -> str | None:
        if not reference or reference.startswith("@") or "{{" in reference:
            return None
        reference_path = reference.replace("\\", "/").lstrip("/")
        if not Path(reference_path).suffix:
            reference_path += ".twig"

        source_dir = Path(source_path).parent.as_posix()
        candidates = {
            (Path(source_dir) / reference_path).as_posix(),
            (Path("resources/views") / reference_path).as_posix(),
            reference_path,
        }
        exact = candidates.intersection(self.paths)
        if len(exact) == 1:
            return next(iter(exact))
        if len(exact) > 1:
            return None

        suffix = "/" + reference_path
        matches = [path for path in self.paths if path.endswith(suffix)]
        return matches[0] if len(matches) == 1 else None


def _child(node: Node, kind: str) -> Node | None:
    return next((child for child in node.named_children if child.type == kind), None)


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _string_value(node: Node | None, source: bytes) -> str | None:
    if node is None:
        return None
    if node.type in {"argument", "argument_value"}:
        child = next(iter(node.named_children), None)
        return _string_value(child, source)
    if node.type != "string":
        return None
    content = _child(node, "string_content")
    if content is not None:
        return node_text(content, source)
    text = node_text(node, source)
    if len(text) < 2 or text[0] not in {"'", '"'} or text[-1] != text[0]:
        return None
    value = text[1:-1]
    return None if "#{" in value else value


def _tag_name(node: Node, source: bytes) -> str | None:
    tag = next((item for item in _walk(node) if item.type in {"tag", "conditional", "repeat"}), None)
    return node_text(tag, source).strip() if tag is not None else None


def _statement_name(node: Node, source: bytes) -> str | None:
    if node.type in {"tag_statement", "from_statement", "import_statement"}:
        tag = _tag_name(node, source)
        if tag == "block":
            variable = _child(node, "variable")
            return node_text(variable, source).strip() if variable is not None else tag
        return tag
    if node.type == "macro_statement":
        method = _child(node, "method")
        return node_text(method, source).strip() if method is not None else "macro"
    if node.type == "assignment_statement":
        variable = _child(node, "variable")
        return node_text(variable, source).strip() if variable is not None else "set"
    return _tag_name(node, source)


def _template_references(root: Node, source: bytes) -> list[str]:
    references: list[str] = []
    for node in _walk(root):
        if node.type in {"tag_statement", "from_statement", "import_statement"}:
            tag = _tag_name(node, source)
            if tag in _TEMPLATE_REFERENCE_TAGS:
                value = next(
                    (found for child in node.named_children
                     if (found := _string_value(child, source)) is not None),
                    None,
                )
                if value:
                    references.append(value)
        elif node.type == "function_call":
            function = _child(node, "function_identifier")
            if function is not None and node_text(function, source) == "include":
                for argument in _walk(node):
                    value = _string_value(argument, source)
                    if value is not None:
                        references.append(value)
                        break
    return list(dict.fromkeys(references))


def _statements(root: Node, source: bytes, path: str, parent_id: str, limit: int) -> list[Statement]:
    statements: list[Statement] = []
    seen: set[str] = set()
    for wrapper in _walk(root):
        if wrapper.type == "output_directive":
            statement_node: Node | None = wrapper
        elif wrapper.type == "comment":
            statement_node = wrapper
        elif wrapper.type == "statement_directive":
            statement_node = next(
                (child for child in wrapper.named_children
                 if child.type in _CAPTURED_DIRECTIVES),
                None,
            )
            if statement_node is None:
                continue
        else:
            continue
        if statement_node is None:
            continue

        start, end = line_span(wrapper)
        text = node_text(wrapper, source).strip()
        if len(text) > limit:
            text = text[:limit]
        name = (
            _statement_name(statement_node, source)
            if statement_node.type != "output_directive"
            else None
        )
        if statement_node.type == "output_directive":
            variable = next(
                (part for part in _walk(statement_node) if part.type == "variable"),
                None,
            )
            name = node_text(variable, source).strip() if variable is not None else None

        statements.append(
            Statement(
                id=disambiguate(
                    statement_id(
                        path,
                        statement_node.start_point[0] + 1,
                        statement_node.start_point[1],
                    ),
                    seen,
                ),
                parentId=parent_id,
                nodeType=statement_node.type,
                text=text,
                name=name,
                startLine=start,
                endLine=end,
                path=path,
            )
        )
    return statements


class TwigParser(BaseParser):
    name = "twig"
    extensions: tuple[str, ...] = (".twig",)
    template_extensions: tuple[str, ...] = (".twig",)
    schema_version = SCHEMA_VERSION
    statement_types = sorted(_CAPTURED_DIRECTIVES | {"comment", "output_directive"})
    frameworks = ["twig"]

    def build_index(
        self, repo_root: Path, files: Sequence[Path], jobs: int = 1
    ) -> TwigTemplateIndex:
        return TwigTemplateIndex(
            frozenset(
                repo_relative(path, repo_root).replace("\\", "/")
                for path in files
                if path.suffix.lower() == ".twig"
            )
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("twig", ctx.source, ctx.parse_timeout_micros).root_node
        path = ctx.path.replace("\\", "/")
        fid = file_id(path)
        index = ctx.resolution_index
        imports = (
            [
                resolved
                for reference in _template_references(root, ctx.source)
                if (resolved := index.resolve(path, reference)) is not None
            ]
            if isinstance(index, TwigTemplateIndex)
            else []
        )
        statements = (
            _statements(root, ctx.source, path, fid, ctx.statement_text_limit)
            if ctx.capture_statements
            else []
        )
        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="twig",
            loc=count_loc(ctx.source.decode("utf-8", "replace")),
            framework="twig",
            uiRole="template",
            importFiles=list(dict.fromkeys(imports)),
            statements=statements,
        )
