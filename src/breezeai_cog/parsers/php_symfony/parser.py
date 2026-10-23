"""Symfony framework parser with route-attribute detection."""

from __future__ import annotations

import re

from ...schemas import FileRecord, Statement
from ..base import ParseContext
from ..php.index import PhpIndex
from ..php.parser import PhpParser
from ..treesitter import parse_source
from .routes import (
    detect_symfony_events,
    detect_symfony_routes,
    detect_symfony_route_builders,
    strip_route_decorators,
)


class SymfonyParser(PhpParser):
    name = "php-symfony"
    priority = 40
    frameworks = ["symfony"]

    def claims(self, path: str, source: bytes) -> bool:
        return (
            # Symfony source and applications using Symfony components need the Symfony
            # semantic layer even when they do not contain a Route attribute.
            b"namespace Symfony\\" in source
            or b"use Symfony\\" in source
            or b"Symfony\\Component\\Routing\\Attribute\\Route" in source
            or b"Symfony\\Component\\Routing\\Annotation\\Route" in source
            or b"Symfony\\Component\\Routing\\" in source and b"#[Route" in source
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx, framework="symfony")
        record.framework = "symfony"
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            index = ctx.resolution_index if isinstance(ctx.resolution_index, PhpIndex) else None
            route_names = detect_symfony_routes(root, ctx.source, ctx.path, record, index)
            detect_symfony_route_builders(root, ctx.source, ctx.path, record)
            detect_symfony_events(root, ctx.source, ctx.path, record)
            _remove_shared_false_positives(record, ctx.source)
            if route_names:
                strip_route_decorators(record, route_names)
        return record


def _clear_semantic(statement: Statement) -> None:
    """Remove a shared semantic label when Symfony-local evidence disproves it."""

    statement.semanticType = None
    statement.framework = None
    statement.method = None
    statement.endpoint = None
    statement.dataAccessHint = None


def _remove_shared_false_positives(record: FileRecord, source: bytes) -> None:
    """Defend Symfony captures from generic PHP call-name collisions.

    The shared detectors intentionally operate without framework-specific type information.
    Their short-name matches are especially unsafe in Symfony's large component/test tree:
    output/session ``fetch``/``get`` calls and container/cache methods are not HTTP or DB
    operations. Keep this narrow and evidence-based; unresolved calls remain unclassified.
    """

    source_text = source.decode("utf-8", "replace").lower()
    has_doctrine = "doctrine\\" in source_text or "doctrine" in source_text
    has_codeigniter = "codeigniter" in source_text
    has_typeorm = "typeorm" in source_text
    has_sequelize = "sequelize" in source_text
    has_dynamodb = "dynamodb" in source_text

    for statement in record.statements:
        text = (statement.text or "").lower()
        if statement.semanticType == "api_call":
            # ``fetch`` is a normal output/test-buffer method in PHP. ``get`` is also
            # common on sessions, flash bags, containers and caches. Preserve calls whose
            # receiver visibly identifies an HTTP client.
            receiver_is_http = any(
                marker in text for marker in ("http", "guzzle", "restclient", "curl", "apiclient")
            )
            if ("->fetch(" in text or "?->fetch(" in text) and not receiver_is_http:
                _clear_semantic(statement)
            elif (
                re.search(r"(?:->|\?->)get\s*\(", text)
                and any(marker in text for marker in ("session", "flashbag", "output", "container", "cache"))
                and not receiver_is_http
            ):
                _clear_semantic(statement)
            continue

        if statement.semanticType != "db_method_call":
            continue
        hint = (statement.dataAccessHint or "").lower()
        if hint == "codeigniter-builder" and not has_codeigniter:
            _clear_semantic(statement)
            continue
        if hint == "typeorm" and not has_typeorm:
            if has_doctrine and re.search(
                r"(?:getrepository|createquerybuilder|findall|find|persist|remove|where|select)", text
            ):
                statement.dataAccessHint = "doctrine"
            else:
                _clear_semantic(statement)
            continue
        if hint == "sequelize" and not has_sequelize:
            if has_doctrine and re.search(r"(?:findall|find|where|select)", text):
                statement.dataAccessHint = "doctrine"
            else:
                _clear_semantic(statement)
            continue
        if hint == "dynamodb" and not has_dynamodb:
            _clear_semantic(statement)
            continue
        if hint == "orm" and not has_doctrine:
            _clear_semantic(statement)
