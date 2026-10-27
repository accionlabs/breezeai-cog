"""Spring Boot framework parser: route detection across v2 (javax) and v3 (jakarta)."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.java_springboot.parser import SpringBootParser
from breezeai_cog.parsers.java_springboot.routes import spring_version
from breezeai_cog.schemas import FileRecord

SRC_V3 = b'''package com.acme.orders;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/orders")
public class OrderController {
    @GetMapping("/{id}")
    public Order get(@PathVariable Long id) { return null; }

    @PostMapping
    public Order create(@RequestBody @Valid OrderDto dto) { return null; }

    @RequestMapping(value="/legacy", method=RequestMethod.PUT)
    public void legacy() {}
}
'''

SRC_V2 = b'''package com.acme.web;
import javax.servlet.http.HttpServletRequest;
import org.springframework.web.bind.annotation.*;

@Controller
@RequestMapping("/v2")
public class PingController {
    @GetMapping("/ping")
    public String ping() { return "ok"; }
}
'''


def _parse(tmp_path, src: bytes, name: str, *, capture: bool = True) -> FileRecord:
    p = tmp_path / name
    p.write_text(src.decode())
    ctx = ParseContext(path=name, abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=capture)
    return SpringBootParser().parse_file(ctx)


def test_routes_require_capture_statements(tmp_path) -> None:
    # Routes are statements — only emitted with statement capture (spec A4).
    rec = _parse(tmp_path, SRC_V3, "OrderController.java", capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_v3_routes(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_V3, "OrderController.java")
    routes = {(s.method, s.endpoint): s for s in rec.statements if s.semanticType == "route"}
    assert ("GET", "/api/orders/{id}") in routes
    assert ("POST", "/api/orders") in routes
    assert ("PUT", "/api/orders/legacy") in routes
    assert all(r.framework == "spring" for r in routes.values())
    fn_ids = {f.id for f in rec.functions}
    assert all(r.parentId in fn_ids for r in routes.values())  # parented to handler methods
    assert rec.framework == "spring"
    assert spring_version(rec) == 3  # jakarta.* -> Spring Boot 3


def test_v2_routes(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_V2, "PingController.java")
    routes = {(s.method, s.endpoint) for s in rec.statements if s.semanticType == "route"}
    assert routes == {("GET", "/v2/ping")}
    assert spring_version(rec) == 2  # javax.* -> Spring Boot 2


def test_route_attributes(tmp_path) -> None:
    # spec C5 — @RequestBody type → requestDTO, return type → responseDTO, isRegex False.
    rec = _parse(tmp_path, SRC_V3, "OrderController.java")
    routes = {s.handler: s for s in rec.statements if s.semanticType == "route"}
    assert routes["create"].requestDTO == "OrderDto"
    assert routes["create"].responseDTO == "Order"
    assert routes["create"].isRegex is False
    assert routes["get"].requestDTO is None  # only @PathVariable, no body


def test_non_controller_has_no_routes(tmp_path) -> None:
    src = b"package x;\npublic class Plain { public int add(int a){ return a; } }\n"
    rec = _parse(tmp_path, src, "Plain.java")
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, SRC_V3, "OrderController.java")
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


def test_claims_selects_spring() -> None:
    registry.clear()
    from breezeai_cog.parsers.java.parser import JavaParser

    registry.register(JavaParser())
    registry.register(SpringBootParser())
    spring = b"import org.springframework.web.bind.annotation.RestController;"
    assert registry.select("X.java", spring).name == "java-springboot"
    assert registry.select("X.java", b"package x;").name == "java"  # plain Java -> base
    registry.clear()


_FN_SRC = b'''package com.example;
import org.springframework.web.servlet.function.RouterFunction;
import org.springframework.web.servlet.function.ServerResponse;
import static org.springframework.web.servlet.function.RequestPredicates.GET;
import static org.springframework.web.servlet.function.RequestPredicates.POST;
import static org.springframework.web.servlet.function.RequestPredicates.contentType;
import static org.springframework.web.servlet.function.RequestPredicates.accept;
import static org.springframework.web.servlet.function.RouterFunctions.route;

public class RouterConfig {
    public RouterFunction<ServerResponse> productRoutes(ProductHandler h) {
        return route(GET("/api/products"), h::list)
                .andRoute(POST("/api/products").and(contentType(JSON)), h::create)
                .andRoute(GET("/api/products/{id}"), h::getById);
    }
    public RouterFunction<ServerResponse> catalogRoutes(ProductHandler h) {
        return route()
                .nest(accept(JSON), b -> b
                        .GET("/api/v2/catalog", h::list)
                        .POST("/api/v2/catalog", h::create))
                .build();
    }
}
'''


def test_functional_router_routes(tmp_path) -> None:
    # #2: WebMvc.fn functional routing — static route()/andRoute() + nested builder DSL.
    rec = _parse(tmp_path, _FN_SRC, "RouterConfig.java")
    got = {(s.method, s.endpoint, s.handler) for s in rec.statements if s.semanticType == "route"}
    assert ("GET", "/api/products", "list") in got
    assert ("POST", "/api/products", "create") in got          # composed .and(contentType)
    assert ("GET", "/api/products/{id}", "getById") in got
    assert ("GET", "/api/v2/catalog", "list") in got           # nested builder form
    assert ("POST", "/api/v2/catalog", "create") in got
    assert all(s.framework == "spring" for s in rec.statements if s.semanticType == "route")


_FN_HANDLERS = b"""package demo;
import org.springframework.context.annotation.Bean;
import org.springframework.web.servlet.function.RouterFunction;
import static org.springframework.web.servlet.function.RouterFunctions.route;
import static org.springframework.web.servlet.function.RequestPredicates.*;

public class Routes {
    @Bean
    public RouterFunction<?> r(Handler h) {
        return route(GET("/a"), this::own)
                .andRoute(GET("/b"), h::other)
                .andRoute(GET("/c"), req -> ok().build());
    }

    ServerResponse own(ServerRequest req) { return null; }
}
"""


def test_functional_handler_line_is_the_declaration_or_the_lambda(tmp_path) -> None:
    rec = _parse(tmp_path, _FN_HANDLERS, "Routes.java")
    got = {s.endpoint: (s.handler, s.handlerLine) for s in rec.statements if s.semanticType == "route"}
    assert got == {
        "/a": ("own", 15),     # this::own — declared in this class
        "/b": ("other", None),  # h::other — a method of another class: no line
        "/c": (None, 12),      # lambda: no name, its line
    }


def test_functional_routing_gated(tmp_path) -> None:
    # A Spring file without RouterFunction must not trigger the functional walk.
    src = b"package x; import org.springframework.stereotype.Service;\n@Service class S { void GET(String p){} }"
    rec = _parse(tmp_path, src, "S.java")
    assert [s for s in rec.statements if s.semanticType == "route"] == []


_ARRAY_SRC = b'''package com.example;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.GetMapping;

@RestController
@RequestMapping("/orders")
public class ReportController {
    @GetMapping({"/report.*", "/report/{fmt}"})
    public String report() { return "r"; }

    @GetMapping("/x/{id:[0-9]{1,3}}")
    public String ranged() { return "x"; }
}
'''


def test_array_path_mapping_splits(tmp_path) -> None:
    # #4: @GetMapping({"/a", "/b"}) -> one route per path; a single regex path with a
    # comma ({1,3} quantifier) is NOT split.
    rec = _parse(tmp_path, _ARRAY_SRC, "ReportController.java")
    eps = {(s.method, s.endpoint) for s in rec.statements if s.semanticType == "route"}
    assert ("GET", "/orders/report.*") in eps
    assert ("GET", "/orders/report/{fmt}") in eps
    assert ("GET", "/orders/x/{id:[0-9]{1,3}}") in eps   # single path, comma preserved
    assert not any('{"' in ep or ", " in ep for _, ep in eps)  # no raw array literal leaked


# ── Context path (server.servlet.context-path) and functional nest prefixes ──────────────────
_CTRL = (
    "package a;\nimport org.springframework.web.bind.annotation.*;\n"
    "@RestController\npublic class C {\n  @GetMapping(\"/x\")\n  public String x() { return \"\"; }\n}\n"
)


def _endpoints(stmts) -> dict[str, str | None]:
    return {s.path: s.endpoint for s in stmts if s.semanticType == "route"}


def test_context_path_prefixes_routes_of_its_module(analyze_files) -> None:
    st = analyze_files({
        # module "app": properties in resources and the same value in resources/config
        "app/src/main/java/a/C.java": _CTRL,
        "app/src/main/resources/application.properties": "server.port=8080\nserver.servlet.context-path=/shop\n",
        "app/src/main/resources/config/application.properties": "server.servlet.context-path = /shop\n",
        "app/src/main/resources/application-dev.properties": "server.servlet.context-path=/dev\n",
        "app/src/test/resources/application.properties": "server.servlet.context-path=/test\n",
        # module "yml": nested YAML; a profile document is ignored
        "yml/src/main/java/a/C.java": _CTRL,
        "yml/src/main/resources/application.yml": (
            "server:\n  servlet:\n    context-path: /api\n---\n"
            "spring:\n  config:\n    activate:\n      on-profile: dev\nserver:\n  servlet:\n    context-path: /x\n"
        ),
        # module "legacy": Spring Boot 1.x key
        "legacy/src/main/java/a/C.java": _CTRL,
        "legacy/src/main/resources/application.properties": "server.context-path=/old\n",
        # module "plain": no context path of its own while others set one → unknown
        "plain/src/main/java/a/C.java": _CTRL,
    })
    assert _endpoints(st) == {
        "app/src/main/java/a/C.java": "/shop/x",
        "yml/src/main/java/a/C.java": "/api/x",
        "legacy/src/main/java/a/C.java": "/old/x",
        "plain/src/main/java/a/C.java": None,
    }


def test_unresolvable_context_path_leaves_endpoint_empty(analyze_files) -> None:
    st = analyze_files({
        # base files disagree
        "a/src/main/java/a/C.java": _CTRL,
        "a/src/main/resources/application.properties": "server.servlet.context-path=/one\n",
        "a/src/main/resources/config/application.yml": "server:\n  servlet:\n    context-path: /two\n",
        # a placeholder cog does not resolve
        "b/src/main/java/a/C.java": _CTRL,
        "b/src/main/resources/application.properties": "server.servlet.context-path=${CTX:/b}\n",
    })
    routes = [s for s in st if s.semanticType == "route"]
    assert {s.path: s.endpoint for s in routes} == {
        "a/src/main/java/a/C.java": None, "b/src/main/java/a/C.java": None,
    }
    assert all(s.handler == "x" and s.method == "GET" for s in routes)  # the rest is kept


_FN_NEST = b"""package demo;
import org.springframework.context.annotation.Bean;
import org.springframework.web.servlet.function.RouterFunction;
import static org.springframework.web.servlet.function.RouterFunctions.*;
import static org.springframework.web.servlet.function.RequestPredicates.*;

public class Routes {
    @Bean
    public RouterFunction<?> r(Handler h) {
        return route()
            .nest(path("/v2").and(accept(JSON)), b -> b
                .GET("/catalog", h::list)
                .nest(path("/admin"), a -> a.DELETE("/items/{id}", h::delete)))
            .nest(accept(JSON), b -> b.GET("/plain", h::plain))
            .path("/v3", b -> b.POST("/orders", h::create))
            .nest(path(PREFIX), b -> b.GET("/dyn", h::dyn))
            .build();
    }

    @Bean
    public RouterFunction<?> s(Handler h) {
        return nest(path("/static"), route(GET("/s"), h::s));
    }
}
"""


def test_functional_nest_prefixes_compose(tmp_path) -> None:
    rec = _parse(tmp_path, _FN_NEST, "Routes.java")
    got = {s.handler: s.endpoint for s in rec.statements if s.semanticType == "route"}
    assert got == {
        "list": "/v2/catalog",
        "delete": "/v2/admin/items/{id}",   # nested nests compose
        "plain": "/plain",                  # accept(...) adds nothing
        "create": "/v3/orders",             # builder .path(prefix, ...)
        "dyn": None,                        # path(PREFIX): unresolved prefix → empty
        "s": "/static/s",                   # static RouterFunctions.nest
    }


def test_security_annotations_are_guards(analyze_files) -> None:
    # Class-level guards first, then the method's; @PermitAll / @PostAuthorize are not guards.
    st = analyze_files({"src/main/java/a/Admin.java": """package a;
import org.springframework.web.bind.annotation.*;
import org.springframework.security.access.prepost.*;
import org.springframework.security.access.annotation.Secured;

@RestController
@PreAuthorize("isAuthenticated()")
public class Admin {
  @Secured("ROLE_ADMIN")
  @DeleteMapping("/users/{id}")
  public void delete(String id) {}

  @PostAuthorize("returnObject != null")
  @GetMapping("/users/{id}")
  public String get(String id) { return id; }
}
"""})
    got = {(s.method, tuple(s.guards or ()), s.authRequired)
           for s in st if s.semanticType == "route"}
    assert got == {
        ("DELETE", ("PreAuthorize(isAuthenticated())", "Secured(ROLE_ADMIN)"), True),
        ("GET", ("PreAuthorize(isAuthenticated())",), True),
    }


def test_no_security_annotation_leaves_guards_unset(analyze_files) -> None:
    st = analyze_files({"src/main/java/a/Open.java": """package a;
import org.springframework.web.bind.annotation.*;
@RestController
public class Open {
  @GetMapping("/ping")
  public String ping() { return "ok"; }
}
"""})
    [r] = [s for s in st if s.semanticType == "route"]
    assert (r.guards, r.authRequired) == (None, None)


# ── Global guards: servlet filters and MVC interceptors (decision Q9) ─────────────────────────
_SPRING = "package a;\nimport org.springframework.stereotype.Component;\nimport org.springframework.web.filter.OncePerRequestFilter;\n"


def test_filters_and_interceptors_guard_the_routes_they_cover(analyze_files) -> None:
    m = "app/src/main/java/a/"
    st = analyze_files({
        "app/src/main/resources/application.properties": "server.servlet.context-path=/shop\n",
        m + "AuthFilter.java": _SPRING + "@Component\npublic class AuthFilter extends OncePerRequestFilter {}\n",
        m + "AuditFilter.java": (
            "package a;\nimport jakarta.servlet.Filter;\n"
            '@WebFilter(urlPatterns = {"/api/*"})\npublic class AuditFilter implements Filter {}\n'
        ),
        # a bean filter registered on /admin/* only; another registered on a non-literal pattern
        m + "LegacyFilter.java": _SPRING + "@Component\npublic class LegacyFilter extends OncePerRequestFilter {}\n",
        m + "DynFilter.java": _SPRING + "@Component\npublic class DynFilter extends OncePerRequestFilter {}\n",
        m + "Filters.java": (
            "package a;\nimport org.springframework.boot.web.servlet.FilterRegistrationBean;\n"
            "public class Filters {\n"
            "  FilterRegistrationBean<LegacyFilter> legacy() {\n"
            "    FilterRegistrationBean<LegacyFilter> r = new FilterRegistrationBean<>(new LegacyFilter());\n"
            '    r.addUrlPatterns("/admin/*");\n    return r;\n  }\n'
            "  FilterRegistrationBean<DynFilter> dyn() {\n"
            "    FilterRegistrationBean<DynFilter> r = new FilterRegistrationBean<>(new DynFilter());\n"
            "    r.addUrlPatterns(props.path());\n    return r;\n  }\n}\n"
        ),
        m + "Web.java": (
            "package a;\nimport org.springframework.web.servlet.config.annotation.*;\n"
            "public class Web implements WebMvcConfigurer {\n"
            "  private AuthInterceptor authInterceptor;\n"
            "  public void addInterceptors(InterceptorRegistry registry) {\n"
            '    registry.addInterceptor(new LogInterceptor()).addPathPatterns("/api/**")'
            '.excludePathPatterns("/api/public/**");\n'
            "    registry.addInterceptor(authInterceptor);\n"
            "    registry.addInterceptor(new DynInterceptor()).addPathPatterns(paths);\n"
            "  }\n}\n"
        ),
        m + "C.java": (
            "package a;\nimport org.springframework.web.bind.annotation.*;\n"
            "@RestController\npublic class C {\n"
            '  @GetMapping("/api/orders/{id}") public String order() { return ""; }\n'
            '  @GetMapping("/api/public/info") public String info() { return ""; }\n'
            '  @GetMapping("/admin/stats") public String stats() { return ""; }\n'
            '  @GetMapping("/home") public String home() { return ""; }\n}\n'
        ),
        # another module: none of app's filters or interceptors
        "other/src/main/java/a/D.java": _CTRL,
    })
    got = {(s.endpoint, tuple(s.guards or ()), s.authRequired)
           for s in st if s.semanticType == "route"}
    assert got == {
        ("/shop/api/orders/{id}", ("AuditFilter", "AuthFilter", "LogInterceptor", "AuthInterceptor"), None),
        ("/shop/api/public/info", ("AuditFilter", "AuthFilter", "AuthInterceptor"), None),
        ("/shop/admin/stats", ("AuthFilter", "LegacyFilter", "AuthInterceptor"), None),
        ("/shop/home", ("AuthFilter", "AuthInterceptor"), None),
        # another module without a context path, while app sets one: address unknown
        (None, (), None),
    }


def test_registration_bean_filter_through_a_local_variable(analyze_files) -> None:
    m = "app/src/main/java/a/"
    st = analyze_files({
        m + "Web.java": (
            "package a;\nimport org.springframework.boot.web.servlet.FilterRegistrationBean;\n"
            "public class Web {\n  FilterRegistrationBean timing() {\n"
            "    FilterRegistrationBean registration = new FilterRegistrationBean();\n"
            "    Filter f = new TimingFilter();\n    registration.setFilter(f);\n"
            '    registration.addUrlPatterns("/*");\n    return registration;\n  }\n'
            "  FilterRegistrationBean ctx() {\n"
            "    FilterRegistrationBean registration = new FilterRegistrationBean();\n"
            "    Filter f = new ContextFilter();\n    registration.setFilter(f);\n"
            '    registration.addUrlPatterns(Paths.SERVICES + "/*");\n    return registration;\n  }\n}\n'
        ),
        m + "C.java": _CTRL,
    })
    [r] = [s for s in st if s.semanticType == "route"]
    assert r.guards == ["TimingFilter"]  # ContextFilter's pattern is not a literal → not added



def test_filter_with_should_not_filter_guards_no_route(analyze_files) -> None:
    m = "src/main/java/a/"
    skip = "  protected boolean shouldNotFilter(HttpServletRequest r) { return r.getRequestURI().equals(\"/open\"); }\n"
    st = analyze_files({
        m + "AuthFilter.java": _SPRING + "@Component\npublic class AuthFilter extends OncePerRequestFilter {}\n",
        m + "JwtFilter.java": _SPRING + "@Component\npublic class JwtFilter extends OncePerRequestFilter {\n" + skip + "}\n",
        # registered elsewhere: the registration does not bring it back
        m + "ApiFilter.java": _SPRING + "public class ApiFilter extends OncePerRequestFilter {\n" + skip + "}\n",
        m + "Filters.java": (
            "package a;\nimport org.springframework.boot.web.servlet.FilterRegistrationBean;\n"
            "public class Filters {\n  FilterRegistrationBean<ApiFilter> api() {\n"
            "    FilterRegistrationBean<ApiFilter> r = new FilterRegistrationBean<>(new ApiFilter());\n"
            '    r.addUrlPatterns("/*");\n    return r;\n  }\n}\n'
        ),
        m + "C.java": (
            "package a;\nimport org.springframework.web.bind.annotation.*;\n"
            '@RestController\npublic class C {\n  @GetMapping("/open") public String open() { return ""; }\n}\n'
        ),
    })
    [r] = [s for s in st if s.semanticType == "route"]
    assert (r.guards, r.authRequired) == (["AuthFilter"], None)

# ── Messaging: broker listeners (inbound) and template sends (outbound) ──────────────────────
_MSG = "src/main/java/m/"


def _msg(analyze_files, files: dict[str, str]):
    st = analyze_files(files)
    eps = {(s.handler, s.endpoint, s.framework, s.method, s.routeKind)
           for s in st if s.semanticType == "eventbus_consumer"}
    sends = {(s.path.rsplit("/", 1)[-1], s.startLine, s.endpoint, s.framework)
             for s in st if s.semanticType == "eventbus_send"}
    return eps, sends


def test_listeners_one_binding_per_address(analyze_files) -> None:
    eps, _ = _msg(analyze_files, {
        "src/main/resources/application.yml": "app:\n  topics:\n    orders: orders.v1\n",
        f"{_MSG}Topics.java": (
            "package m;\npublic class Topics {\n"
            '  public static final String BILLING = "billing";\n}\n'
        ),
        f"{_MSG}L.java": (
            "package m;\n"
            "import org.springframework.kafka.annotation.KafkaListener;\n"
            "import org.springframework.amqp.rabbit.annotation.RabbitListener;\n"
            "import org.springframework.jms.annotation.JmsListener;\n"
            "import io.awspring.cloud.sqs.annotation.SqsListener;\n"
            "import org.springframework.stereotype.Component;\n"
            "@Component\npublic class L {\n"
            '  private static final String AUDIT = "audit";\n'
            '  @KafkaListener(topics = {"a", "b"}, groupId = "g")\n  public void two(String m) {}\n'
            '  @KafkaListener(topics = "${app.topics.orders}")\n  public void prop(String m) {}\n'
            '  @KafkaListener(topics = "${missing.key}")\n  public void unknown(String m) {}\n'
            '  @KafkaListener(topicPattern = "evt.*")\n  public void pattern(String m) {}\n'
            "  @RabbitListener(queues = Topics.BILLING)\n  public void bill(String m) {}\n"
            "  @JmsListener(destination = AUDIT)\n  public void audit(String m) {}\n"
            '  @SqsListener("jobs")\n  public void job(String m) {}\n'
            "}\n"
        ),
    })
    assert eps == {
        ("two", "a", "kafka", "EVENT", "message"),
        ("two", "b", "kafka", "EVENT", "message"),
        ("prop", "orders.v1", "kafka", "EVENT", "message"),   # ${property} from base config
        ("unknown", None, "kafka", "EVENT", "message"),       # unresolved → empty, never the key
        ("pattern", None, "kafka", "EVENT", "message"),       # a pattern is not one address
        ("bill", "billing", "rabbitmq", "MESSAGE", "message"),  # Class.FIELD constant
        ("audit", "audit", "jms", "MESSAGE", "message"),        # same-class constant
        ("job", "jobs", "aws-sqs", "MESSAGE", "message"),
    }


def test_class_level_listener_binds_its_handler_methods(analyze_files) -> None:
    eps, _ = _msg(analyze_files, {
        f"{_MSG}C.java": (
            "package m;\n"
            "import org.springframework.kafka.annotation.KafkaHandler;\n"
            "import org.springframework.kafka.annotation.KafkaListener;\n"
            '@KafkaListener(topics = "multi")\npublic class C {\n'
            "  @KafkaHandler\n  public void onA(String a) {}\n"
            "  @KafkaHandler\n  public void onB(Integer b) {}\n"
            "  public void helper() {}\n}\n"
        ),
    })
    assert {(h, e) for h, e, *_ in eps} == {("onA", "multi"), ("onB", "multi")}


def test_template_sends(analyze_files) -> None:
    _, sends = _msg(analyze_files, {
        f"{_MSG}S.java": (
            "package m;\n"
            "import org.springframework.kafka.core.KafkaTemplate;\n"
            "import org.springframework.amqp.rabbit.core.RabbitTemplate;\n"
            "import org.springframework.jms.core.JmsTemplate;\n"
            "import org.springframework.stereotype.Service;\n"
            "@Service\npublic class S {\n"
            "  private final KafkaTemplate<String, String> kafka;\n"
            "  private final RabbitTemplate rabbit;\n"
            "  private final JmsTemplate jms;\n"
            "  S(KafkaTemplate<String, String> kafka, RabbitTemplate rabbit, JmsTemplate jms) {\n"
            "    this.kafka = kafka; this.rabbit = rabbit; this.jms = jms;\n  }\n"
            "  void go(String topic, Other other) {\n"
            '    kafka.send("orders", "x");\n'                      # 15
            "    kafka.send(topic, \"x\");\n"                       # 16 runtime → empty
            "    kafka.sendDefault(\"x\");\n"                        # 17 default → empty
            '    rabbit.convertAndSend("billing", "x");\n'          # 18 default exchange
            '    rabbit.convertAndSend("", "billing", "x");\n'      # 19 "" exchange → queue
            '    rabbit.convertAndSend("events", "rk", "x");\n'     # 20 exchange
            '    this.jms.convertAndSend("audit", "x");\n'          # 21
            '    other.send("orders", "x");\n'                      # 22 not a template → none
            "  }\n}\n"
        ),
    })
    assert sends == {
        ("S.java", 15, "orders", "kafka"),
        ("S.java", 16, None, "kafka"),
        ("S.java", 17, None, "kafka"),
        ("S.java", 18, "billing", "rabbitmq"),
        ("S.java", 19, "billing", "rabbitmq"),
        ("S.java", 20, "events", "rabbitmq"),
        ("S.java", 21, "audit", "jms"),
    }


def test_module_without_context_path_is_unknown_only_when_another_sets_one(analyze_files) -> None:
    alone = analyze_files({"web/src/main/java/a/C.java": _CTRL})
    assert _endpoints(alone) == {"web/src/main/java/a/C.java": "/x"}  # no prefix anywhere
    split = analyze_files({
        "web/src/main/java/a/C.java": _CTRL,
        "boot/src/main/java/a/App.java": "package a;\npublic class App {}\n",
        "boot/src/main/resources/application.properties": "server.servlet.context-path=/shop\n",
    })
    # the context path is set in the boot module: web's routes may be served under it
    assert _endpoints(split) == {"web/src/main/java/a/C.java": None}
