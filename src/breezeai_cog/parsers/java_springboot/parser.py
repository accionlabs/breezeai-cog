"""SpringBootParser — a Java framework parser. Selected (one parser per file) over
JavaParser when ``claims`` finds an ``org.springframework`` import; reuses
``JavaParser.extract`` (single parse), then detects Spring MVC routes from the captured
annotations. Covers Spring Boot v2 and v3 (identical web annotations). The module's
``server.servlet.context-path`` (the ``spring.context-paths`` fact) prefixes every HTTP route except
WebFlux.fn routes, which take ``spring.webflux.base-path`` instead. Broker
listeners and template sends (Kafka, RabbitMQ, JMS, SQS) are detected in ``messaging.py``,
``@Scheduled`` jobs in ``scheduling.py``."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..constfold import resolve_all
from ..emits import apply
from ..java.config import module_of, resolve_placeholders
from ..java.constants import collect_constants
from ..java.imports import JavaIndex
from ..java.parser import JavaParser
from ..java.web_guards import module_guards
from ..treesitter import parse_source
from .config_facts import context_paths, module_properties
from .functional_routes import detect_spring_functional_routes
from .messaging import detect_spring_listeners, detect_spring_sends, has_messaging
from .scheduling import detect_spring_scheduled, has_scheduled
from .queries import detect_spring_queries
from .routes import apply_context_path, apply_global_guards, detect_spring_routes


#: A WebFlux.fn router (``org.springframework.web.reactive.function.server``), as opposed to
#: WebMvc.fn (``…web.servlet.function``).
_WEBFLUX_FN = b"org.springframework.web.reactive.function"
_WEBFLUX_BASE_PATH = "spring.webflux.base-path"


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
            idx = ctx.resolution_index
            module = module_of(ctx.path)
            routes = detect_spring_routes(record, root, ctx.source)  # annotated controllers
            reactive = []
            if b"RouterFunction" in ctx.source:  # functional routing needs the AST
                functional = detect_spring_functional_routes(
                    root, ctx.source, ctx.path, record, idx)
                if _WEBFLUX_FN in ctx.source:  # WebFlux.fn: not served by the servlet
                    reactive = functional
                else:  # WebMvc.fn
                    routes += functional
            if routes:
                # Servlet filters and MVC interceptors of this module run first (matched within
                # the servlet context, so before the context path is joined).
                guards = module_guards(idx)
                if module is not None and module in guards:
                    apply_global_guards(routes, guards[module])
                # server.servlet.context-path of this file's module prefixes every HTTP route.
                # A module without one, in a repo where another module sets one, may be served
                # by that module's application: its address is unknown.
                paths = context_paths(idx)
                if module is not None and module in paths:
                    apply_context_path(routes, paths[module])
                elif paths:
                    apply_context_path(routes, None)
            if reactive and module is not None:
                # WebFlux ignores the servlet context path and filters; its prefix is its own.
                props = module_properties(idx).get(module, {})
                if _WEBFLUX_BASE_PATH in props:
                    base = props[_WEBFLUX_BASE_PATH]
                    apply_context_path(
                        reactive, resolve_placeholders(base, props) if base is not None else None)
            if routes or reactive:
                apply(record, routes + reactive, ctx.source, ctx.path)
                record.framework = "spring"
            messaging, scheduled = has_messaging(ctx.source), has_scheduled(ctx.source)
            if messaging or scheduled:  # @KafkaListener … consumers, template sends; @Scheduled
                consts = resolve_all(collect_constants(root, ctx.source),
                                     base=idx.consts if isinstance(idx, JavaIndex) else {})
                props = module_properties(idx).get(module, {}) if module is not None else {}
                found = False
                if messaging:
                    listeners = detect_spring_listeners(
                        root, ctx.source, ctx.path, record, consts, props)
                    apply(record, listeners, ctx.source, ctx.path)
                    sends = detect_spring_sends(root, ctx.source, consts, props)
                    apply(record, sends, ctx.source, ctx.path)
                    found = bool(listeners or sends)
                if scheduled:
                    timers = detect_spring_scheduled(
                        root, ctx.source, ctx.path, record, consts, props)
                    apply(record, timers, ctx.source, ctx.path)
                    found = found or bool(timers)
                if found and record.framework is None:
                    record.framework = "spring"
            # @Query → query_statement
            apply(record, detect_spring_queries(record, root, ctx.source), ctx.source, ctx.path)
        return record
