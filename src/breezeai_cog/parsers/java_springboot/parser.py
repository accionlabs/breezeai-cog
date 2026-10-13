"""SpringBootParser — a Java framework parser. Selected (one parser per file) over
JavaParser when ``claims`` finds an ``org.springframework`` import; reuses
``JavaParser.extract`` (single parse), then detects Spring MVC routes from the captured
annotations. Covers Spring Boot v2 and v3 (identical web annotations). The module's
``server.servlet.context-path`` (read by the Java index) prefixes every HTTP route. Broker
listeners and template sends (Kafka, RabbitMQ, JMS, SQS) are detected in ``messaging.py``,
``@Scheduled`` jobs in ``scheduling.py``."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..constfold import resolve_all
from ..java.config import module_of
from ..java.constants import collect_constants
from ..java.imports import JavaIndex
from ..java.parser import JavaParser
from ..treesitter import parse_source
from .functional_routes import detect_spring_functional_routes
from .messaging import detect_spring_listeners, detect_spring_sends, has_messaging
from .scheduling import detect_spring_scheduled, has_scheduled
from .queries import detect_spring_queries
from .routes import apply_context_path, apply_global_guards, detect_spring_routes


class SpringBootParser(JavaParser):
    name = "java-springboot"
    priority = 10
    frameworks = ["spring", "springboot"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"org.springframework" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("java", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited Java extraction (one parse)
        if ctx.capture_statements:  # routes/queries are statements — gated by statement capture
            routes = detect_spring_routes(record)  # annotated controllers — off the record
            if b"RouterFunction" in ctx.source:  # functional routing needs the AST
                routes += detect_spring_functional_routes(root, ctx.source, ctx.path, record)
            if routes:
                idx = ctx.resolution_index
                module = module_of(ctx.path)
                # Servlet filters and MVC interceptors of this module run first (matched within
                # the servlet context, so before the context path is joined).
                guards = idx.module_guards if isinstance(idx, JavaIndex) else {}
                if module is not None and module in guards:
                    apply_global_guards(routes, guards[module])
                # server.servlet.context-path of this file's module prefixes every HTTP route
                paths = idx.context_paths if isinstance(idx, JavaIndex) else {}
                if module is not None and module in paths:
                    apply_context_path(routes, paths[module])
                record.statements.extend(routes)
                record.framework = "spring"
            messaging, scheduled = has_messaging(ctx.source), has_scheduled(ctx.source)
            if messaging or scheduled:  # @KafkaListener … consumers, template sends; @Scheduled
                idx = ctx.resolution_index
                module = module_of(ctx.path)
                consts = resolve_all(collect_constants(root, ctx.source),
                                     base=idx.consts if isinstance(idx, JavaIndex) else {})
                props = (idx.module_properties.get(module or "", {})
                         if isinstance(idx, JavaIndex) and module is not None else {})
                found = False
                if messaging:
                    listeners = detect_spring_listeners(
                        root, ctx.source, ctx.path, record, consts, props)
                    record.statements.extend(listeners)
                    sent = detect_spring_sends(root, ctx.source, ctx.path, record, consts, props)
                    found = bool(listeners or sent)
                if scheduled:
                    timers = detect_spring_scheduled(
                        root, ctx.source, ctx.path, record, consts, props)
                    record.statements.extend(timers)
                    found = found or bool(timers)
                if found and record.framework is None:
                    record.framework = "spring"
            record.statements.extend(detect_spring_queries(record))  # @Query → query_statement
        return record
