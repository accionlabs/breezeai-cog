"""TerraformParser — parses ``.tf`` and ``.tfvars`` files into ``type="config"`` FileRecords.

Each top-level HCL block becomes a Statement (gated by ``--capture-statements``) whose
``text`` is the verbatim block source.  This makes blocks directly filterable via the MCP
``get_nodes_by_label`` tool (e.g. ``filterby[text][containsi]="azure"``).

``module`` blocks additionally yield Class records (always, not gated) whose
``constructorParams`` are the input variables passed to the module — the IaC equivalent of
dependency-injected constructor parameters.

Module and provider sources are also surfaced as ``externalImports`` (always, ungated),
so the file-level graph retains dependency edges regardless of the statements flag.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from tree_sitter import Node

from ...emit import class_id, disambiguate, file_id, statement_id
from ...schemas import SCHEMA_VERSION, FileRecord, Statement
from ...schemas.capture import Class, ConstructorParam
from ...utils import count_loc
from ..base import BaseParser, ParseContext
from ..treesitter import node_text, parse_source
from .mappings import FRAMEWORKS, STATEMENT_TYPES


# ── semantic type mapping ─────────────────────────────────────────────────────
# Only behaviour-bearing block types get a semanticType.  Structure-only blocks
# (variable / output / locals / provider / terraform) carry nodeType only.

_SEMANTIC_TYPE: dict[str, str | None] = {
    "resource": "iac_resource",
    "data": "iac_data",
    "module": "iac_module",
    "provider": None,
    "variable": None,
    "output": None,
    "locals": None,
    "terraform": None,
}

# Terraform meta-arguments in module blocks — NOT constructor params
_MODULE_META_ARGS: frozenset[str] = frozenset(
    {"source", "version", "count", "for_each", "providers", "depends_on"}
)

# ── platform detection ────────────────────────────────────────────────────────

_CLOUD_PREFIXES: dict[str, str] = {
    "aws_": "aws",
    "azurerm_": "azure",
    "google_": "google",
}

_PROVIDER_PLATFORMS: dict[str, str] = {
    "aws": "aws",
    "azurerm": "azure",
    "google": "google",
}


def _block_platform(keyword: str, labels: list[str]) -> str | None:
    """Derive cloud provider from the block's resource type prefix or provider name."""
    if keyword in ("resource", "data") and labels:
        resource_type = labels[0]
        for prefix, platform in _CLOUD_PREFIXES.items():
            if resource_type.startswith(prefix):
                return platform
    elif keyword == "provider" and labels:
        return _PROVIDER_PLATFORMS.get(labels[0])
    return None


def _dominant_platform(statements: list[Statement]) -> str | None:
    """Return the most frequent non-None platform across statements, or None."""
    platforms = [s.platform for s in statements if s.platform is not None]
    if not platforms:
        return None
    return Counter(platforms).most_common(1)[0][0]


# ── AST helpers ───────────────────────────────────────────────────────────────


def _iter_nodes(node: Node):
    """Depth-first walk of every node in a subtree."""
    yield node
    for child in node.children:
        yield from _iter_nodes(child)


def _label_text(node: Node, source: bytes) -> str:
    """Extract the string content from a ``string_lit`` label node."""
    for child in node.children:
        if child.type == "template_literal":
            return node_text(child, source)
    return node_text(node, source).strip('"')


def _expr_str(node: Node, source: bytes) -> str | None:
    """Return the string value of an ``expression`` if it is a simple string literal."""
    if node.type != "expression":
        return None
    for child in node.named_children:
        if child.type == "literal_value":
            for lit in child.named_children:
                if lit.type == "string_lit":
                    return _label_text(lit, source)
    return None


def _object_get(expr: Node, key: str, source: bytes) -> str | None:
    """Get the string value of *key* from an HCL object expression."""
    for node in _iter_nodes(expr):
        if node.type == "object_elem":
            kids = node.named_children
            if len(kids) >= 2:
                k = node_text(kids[0], source).strip('"')
                if k == key:
                    return _expr_str(kids[1], source)
    return None


def _infer_value_type(expr_node: Node, source: bytes) -> str:
    """Infer the HCL type of a value expression from its AST structure."""
    if expr_node.type != "expression":
        return "any"
    for child in expr_node.named_children:
        if child.type != "literal_value":
            continue
        for lit in child.named_children:
            if lit.type == "string_lit":
                return "string"
            if lit.type == "numeric_lit":
                return "number"
        raw = node_text(child, source).strip()
        if raw in ("true", "false"):
            return "bool"
    return "any"


def _block_name(keyword: str, labels: list[str]) -> str | None:
    """The ``name`` for a Statement: resource type for resource/data, instance name otherwise."""
    if not labels:
        return None
    return labels[0]  # resource type OR module/variable/output/provider name


def _terraform_address(keyword: str, labels: list[str]) -> str | None:
    """The Terraform resource address used as Statement ``endpoint``.

    This is the join key: a reference ``aws_s3_bucket.assets`` resolves to the block
    whose ``endpoint`` is ``"aws_s3_bucket.assets"``.
    """
    if keyword == "resource" and len(labels) >= 2:
        return f"{labels[0]}.{labels[1]}"
    if keyword == "data" and len(labels) >= 2:
        return f"data.{labels[0]}.{labels[1]}"
    if keyword == "module" and labels:
        return f"module.{labels[0]}"
    return None


def _module_source(body_node: Node, source: bytes) -> str | None:
    """Extract the ``source`` value from a module body.

    Falls back to raw expression text when the source is not a string literal
    (e.g. ``source = var.module_path``), so the dependency is never silently dropped.
    """
    for attr in body_node.named_children:
        if attr.type != "attribute":
            continue
        kids = attr.named_children
        if len(kids) >= 2 and kids[0].type == "identifier" and node_text(kids[0], source) == "source":
            s = _expr_str(kids[1], source)
            if s is not None:
                return s
            raw = node_text(kids[1], source).strip()
            return raw if raw else None
    return None


def _module_constructor_params(body_node: Node, source: bytes) -> list[ConstructorParam]:
    """Extract module input variables from a module body, skipping meta-arguments."""
    params: list[ConstructorParam] = []
    for attr in body_node.named_children:
        if attr.type != "attribute":
            continue
        kids = attr.named_children
        if len(kids) < 2 or kids[0].type != "identifier":
            continue
        attr_name = node_text(kids[0], source)
        if attr_name in _MODULE_META_ARGS:
            continue
        params.append(ConstructorParam(name=attr_name, type=_infer_value_type(kids[1], source)))
    return params


def _required_provider_sources(terraform_body: Node, source: bytes) -> list[str]:
    """Extract provider sources from a ``required_providers`` nested block."""
    sources: list[str] = []
    for child in terraform_body.named_children:
        if child.type != "block":
            continue
        named = child.named_children
        if not named or named[0].type != "identifier":
            continue
        if node_text(named[0], source) != "required_providers":
            continue
        rp_body = next((n for n in named if n.type == "body"), None)
        if rp_body is None:
            continue
        for attr in rp_body.named_children:
            if attr.type != "attribute":
                continue
            kids = attr.named_children
            if len(kids) >= 2:
                src = _object_get(kids[1], "source", source)
                if src:
                    sources.append(src)
    return sources


# ── parser ────────────────────────────────────────────────────────────────────


class TerraformParser(BaseParser):
    name = "terraform"
    extensions = (".tf", ".tfvars")
    schema_version = SCHEMA_VERSION
    statement_types = STATEMENT_TYPES
    frameworks = FRAMEWORKS

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("terraform", ctx.source, ctx.parse_timeout_micros).root_node
        return self.extract(root, ctx)

    def extract(self, root: Node, ctx: ParseContext) -> FileRecord:  # noqa: C901
        """Build a FileRecord from an already-parsed AST root.

        With ``--capture-statements``: each top-level block → one Statement whose
        ``text`` is the verbatim HCL source, making it directly filterable by MCP.
        Without the flag: only ``externalImports`` (module + provider sources) are captured.

        Module blocks always yield a Class record (ungated) whose ``constructorParams``
        are the input variables passed to the module.
        """
        source = ctx.source
        path = ctx.path
        fid = file_id(path)
        is_tfvars = Path(path).suffix == ".tfvars"

        statements: list[Statement] = []
        classes: list[Class] = []
        external_imports: set[str] = set()
        seen_ids: set[str] = set()

        body = next((c for c in root.named_children if c.type == "body"), root)

        if is_tfvars:
            if ctx.capture_statements:
                for attr in body.named_children:
                    if attr.type != "attribute":
                        continue
                    kids = attr.named_children
                    if not kids or kids[0].type != "identifier":
                        continue
                    var_name = node_text(kids[0], source)
                    start = attr.start_point[0] + 1
                    end = attr.end_point[0] + 1
                    sid = disambiguate(statement_id(path, start, 0), seen_ids)
                    seen_ids.add(sid)
                    statements.append(Statement(
                        id=sid,
                        parentId=fid,
                        nodeType="attribute",
                        semanticType="iac_variable_value",
                        text=node_text(attr, source),
                        startLine=start,
                        endLine=end,
                        name=var_name,
                    ))
        else:
            for block in body.named_children:
                if block.type != "block":
                    continue
                named = block.named_children
                if not named or named[0].type != "identifier":
                    continue

                keyword = node_text(named[0], source)
                labels = [_label_text(n, source) for n in named if n.type == "string_lit"]
                body_node = next((n for n in named if n.type == "body"), None)
                start = block.start_point[0] + 1
                end = block.end_point[0] + 1

                # Always collect external module sources (ungated)
                if keyword == "module" and labels and body_node:
                    src = _module_source(body_node, source)
                    if src and not src.startswith(("./", "../")):
                        external_imports.add(src)

                    # Module blocks yield a Class record (ungated — mirrors how code
                    # parsers always capture class structure regardless of statement flag)
                    instance_name = labels[0]
                    cid = disambiguate(class_id(path, instance_name), seen_ids)
                    seen_ids.add(cid)
                    classes.append(Class(
                        id=cid,
                        parentId=fid,
                        name=instance_name,
                        type="module",
                        startLine=start,
                        endLine=end,
                        constructorParams=_module_constructor_params(body_node, source),
                    ))

                # Always collect required_providers sources (ungated)
                if keyword == "terraform" and body_node:
                    for src in _required_provider_sources(body_node, source):
                        external_imports.add(src)

                if ctx.capture_statements:
                    sid = disambiguate(statement_id(path, start, 0), seen_ids)
                    seen_ids.add(sid)
                    statements.append(Statement(
                        id=sid,
                        parentId=fid,
                        nodeType="block",
                        semanticType=_SEMANTIC_TYPE.get(keyword),
                        text=node_text(block, source),
                        startLine=start,
                        endLine=end,
                        name=_block_name(keyword, labels),
                        endpoint=_terraform_address(keyword, labels),
                        platform=_block_platform(keyword, labels),
                    ))

        return FileRecord(
            id=fid,
            path=path,
            type="config",
            language="hcl",
            framework="terraform",
            loc=count_loc(source.decode("utf-8", "replace")),
            externalImports=sorted(external_imports),
            classes=classes,
            statements=statements,
            platform=_dominant_platform(statements),
        )
