"""Spring role table: docs/entry-point-roles/spring.md — one test per row."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point


EP = {"route", "eventbus_consumer", "timer"}
SRC = "src/main/java/a/"


def _eps(stmts, file: str | None = None):
    return [
        s
        for s in stmts
        if is_entry_point(s)
        and (file is None or s.path == SRC + file)
    ]


def _java(name: str, imports: str, body: str) -> dict[str, str]:
    return {f"{SRC}{name}.java": f"package a;\n{imports}\n{body}\n"}


WEB = "import org.springframework.web.bind.annotation.*;"


def test_verb_mapping_on_controller_method_is_binding(analyze_files) -> None:
    st = analyze_files(
        _java(
            "C",
            WEB,
            """@RestController
public class C {
  @GetMapping("/orders/{id}")
  public String get(String id) { return id; }
}""",
        )
    )
    [r] = _eps(st)
    assert (r.semanticType, r.method, r.routeKind, r.endpoint, r.framework) == (
        "route",
        "GET",
        "route",
        "/orders/{id}",
        "spring",
    )
    assert (r.handler, r.handlerLine) == ("get", 5)  # the method, from its annotations


def test_mapping_with_several_paths_is_one_binding_per_path(analyze_files) -> None:
    st = analyze_files(
        _java(
            "C",
            WEB,
            """@RestController
public class C {
  @PostMapping({"/a", "/b"})
  public void create() {}
}""",
        )
    )
    assert sorted(r.endpoint for r in _eps(st)) == ["/a", "/b"]


def test_request_mapping_with_method_on_method_is_binding(analyze_files) -> None:
    st = analyze_files(
        _java(
            "C",
            WEB,
            """@RestController
public class C {
  @RequestMapping(value = "/legacy", method = RequestMethod.PUT)
  public void legacy() {}
}""",
        )
    )
    [r] = _eps(st)
    assert (r.method, r.endpoint, r.handler) == ("PUT", "/legacy", "legacy")


def test_class_request_mapping_is_wiring(analyze_files) -> None:
    st = analyze_files(
        _java(
            "C",
            WEB,
            """@RestController
@RequestMapping("/api")
public class C {
  @GetMapping("/orders")
  public String list() { return ""; }
}""",
        )
    )
    [r] = _eps(st)  # the class annotation has no record of its own
    assert r.endpoint == "/api/orders"


def test_context_path_is_wiring(analyze_files) -> None:
    files = _java(
        "C",
        WEB,
        """@RestController
public class C {
  @GetMapping("/x")
  public String x() { return ""; }
}""",
    )
    files["src/main/resources/application.properties"] = "server.servlet.context-path=/ctx\n"
    [r] = _eps(analyze_files(files))
    assert r.endpoint == "/ctx/x"


FN_IMPORTS = """import org.springframework.context.annotation.Bean;
import org.springframework.web.servlet.function.RouterFunction;
import static org.springframework.web.servlet.function.RouterFunctions.route;
import static org.springframework.web.servlet.function.RequestPredicates.*;"""


def test_functional_route_is_binding(analyze_files) -> None:
    st = analyze_files(
        _java(
            "Fn",
            FN_IMPORTS,
            """public class Fn {
  @Bean
  public RouterFunction<?> r(Handler h) {
    return route(GET("/products"), h::list);
  }
}""",
        )
    )
    [r] = _eps(st)
    assert (r.method, r.endpoint, r.routeKind, r.handler) == ("GET", "/products", "route", "list")


FLUX_IMPORTS = FN_IMPORTS.replace(".web.servlet.function.", ".web.reactive.function.server.")
FN_BODY = """public class Fn {
  @Bean
  public RouterFunction<?> r(Handler h) {
    return route(GET("/products"), h::list);
  }
}"""


def _fn_endpoint(analyze_files, imports: str, config: str) -> str | None:
    files = _java("Fn", imports, FN_BODY)
    files["src/main/resources/application.properties"] = config
    [r] = _eps(analyze_files(files))
    return r.endpoint


def test_servlet_context_path_prefixes_a_webmvc_functional_route(analyze_files) -> None:
    assert _fn_endpoint(analyze_files, FN_IMPORTS,
                        "server.servlet.context-path=/ctx\n") == "/ctx/products"


def test_servlet_context_path_does_not_prefix_a_webflux_functional_route(analyze_files) -> None:
    # WebFlux is not served by the servlet: its context path does not apply.
    assert _fn_endpoint(analyze_files, FLUX_IMPORTS,
                        "server.servlet.context-path=/ctx\n") == "/products"


def test_webflux_base_path_prefixes_a_webflux_functional_route(analyze_files) -> None:
    config = "server.servlet.context-path=/ctx\nspring.webflux.base-path=/api\n"
    assert _fn_endpoint(analyze_files, FLUX_IMPORTS, config) == "/api/products"
    assert _fn_endpoint(analyze_files, FN_IMPORTS, config) == "/ctx/products"


def test_unresolved_webflux_base_path_leaves_the_address_empty(analyze_files) -> None:
    assert _fn_endpoint(analyze_files, FLUX_IMPORTS,
                        "spring.webflux.base-path=${BASE}\n") is None
    assert _fn_endpoint(analyze_files, FLUX_IMPORTS,
                        "base=/b\nspring.webflux.base-path=${base}\n") == "/b/products"


def test_functional_nest_path_is_wiring(analyze_files) -> None:
    st = analyze_files(
        _java(
            "Fn",
            FN_IMPORTS,
            """public class Fn {
  @Bean
  public RouterFunction<?> r(Handler h) {
    return route().nest(path("/v2"), b -> b.GET("/catalog", h::cat)).build();
  }
}""",
        )
    )
    [r] = _eps(st)
    assert r.endpoint == "/v2/catalog"


LISTENERS = """import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.stereotype.Component;"""


def test_message_listener_is_binding(analyze_files) -> None:
    st = analyze_files(
        _java(
            "L",
            LISTENERS,
            """@Component
public class L {
  @KafkaListener(topics = "orders.created")
  public void onOrder(String m) {}
  @RabbitListener(queues = "billing")
  public void onBill(String m) {}
}""",
        )
    )
    got = {(r.semanticType, r.endpoint, r.handler) for r in _eps(st)}
    assert got == {
        ("eventbus_consumer", "orders.created", "onOrder"),
        ("eventbus_consumer", "billing", "onBill"),
    }


def test_scheduled_is_binding(analyze_files) -> None:
    st = analyze_files(
        _java(
            "J",
            "import org.springframework.scheduling.annotation.Scheduled;\n"
            "import org.springframework.stereotype.Component;",
            """@Component
public class J {
  @Scheduled(cron = "0 2 * * *")
  public void nightly() {}
  @Scheduled(fixedRate = 5000)
  public void poll() {}
}""",
        )
    )
    got = {(r.semanticType, r.endpoint, r.handler) for r in _eps(st)}
    assert got == {("timer", "0 2 * * *", "nightly"), ("timer", "5000", "poll")}


def test_scheduled_schedule_is_resolved_or_empty(analyze_files) -> None:
    files = _java(
        "J",
        "import org.springframework.scheduling.annotation.Scheduled;",
        """public class J {
  static final String NIGHTLY = "0 3 * * *";
  @Scheduled(cron = NIGHTLY)
  @Scheduled(fixedDelayString = "${jobs.poll}")
  public void twice() {}
  @Scheduled(cron = "#{@cfg.cron}")
  public void spel() {}
}""",
    )
    files["src/main/resources/application.properties"] = "jobs.poll=30000\n"
    got = sorted((r.endpoint or "", r.handler) for r in _eps(analyze_files(files)))
    # The repeatable annotation gives two bindings; SpEL is unknown → empty, never the code.
    assert got == [("", "spel"), ("0 3 * * *", "twice"), ("30000", "twice")]


def test_method_security_is_guard(analyze_files) -> None:
    st = analyze_files(
        _java(
            "C",
            WEB + "\nimport org.springframework.security.access.prepost.PreAuthorize;",
            """@RestController
public class C {
  @PreAuthorize("hasRole('ADMIN')")
  @DeleteMapping("/orders/{id}")
  public void delete(String id) {}
}""",
        )
    )
    [r] = _eps(st)
    assert r.guards and "PreAuthorize" in r.guards[0]


def test_filter_and_interceptor_have_no_record(analyze_files) -> None:
    st = analyze_files(
        _java(
            "F",
            """import org.springframework.web.filter.OncePerRequestFilter;
import org.springframework.web.servlet.HandlerInterceptor;
import org.springframework.stereotype.Component;""",
            """@Component
public class F extends OncePerRequestFilter implements HandlerInterceptor {
  protected void doFilterInternal(Object req, Object res, Object chain) {}
  public boolean preHandle(Object req, Object res, Object h) { return true; }
}""",
        )
    )
    assert _eps(st) == []


def test_rest_client_call_is_not_entry_point(analyze_files) -> None:
    st = analyze_files(
        _java(
            "S",
            "import org.springframework.web.client.RestTemplate;\n"
            "import org.springframework.stereotype.Service;",
            """@Service
public class S {
  private RestTemplate rest = new RestTemplate();
  public String fetch() { return rest.getForObject("http://billing/api/x", String.class); }
}""",
        )
    )
    assert _eps(st) == []


def test_application_start_up_is_none(analyze_files) -> None:
    st = analyze_files(
        _java(
            "App",
            """import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;""",
            """@SpringBootApplication
public class App {
  public static void main(String[] a) { SpringApplication.run(App.class, a); }
}""",
        )
    )
    assert _eps(st) == []


def test_unmapped_controller_method_is_none(analyze_files) -> None:
    st = analyze_files(
        _java(
            "C",
            WEB,
            """@RestController
public class C {
  @GetMapping("/x")
  public String x() { return helper(); }
  private String helper() { return ""; }
}""",
        )
    )
    assert [r.handler for r in _eps(st)] == ["x"]


def test_functional_route_handler_in_another_class_is_located(analyze_files) -> None:
    # `h::list` is a method of the parameter's type, declared in another file.
    files = _java(
        "Routes",
        "import org.springframework.context.annotation.Bean;\n"
        "import org.springframework.web.servlet.function.RouterFunction;\n"
        "import static org.springframework.web.servlet.function.RouterFunctions.route;\n"
        "import static org.springframework.web.servlet.function.RequestPredicates.GET;",
        "public class Routes {\n  @Bean\n  public RouterFunction<?> r(OrderHandler h) {\n"
        "    return route(GET(\"/orders\"), h::list);\n  }\n}",
    )
    files.update(_java("OrderHandler", "", "public class OrderHandler {\n"
                       "  public Object list(Object req) { return null; }\n}"))
    [r] = _eps(analyze_files(files), "Routes.java")
    assert (r.handler, r.handlerPath, r.handlerLine) == ("list", SRC + "OrderHandler.java", 4)


def test_servlet_filter_does_not_guard_a_webflux_functional_route(analyze_files) -> None:
    files = _java("Fn", FLUX_IMPORTS, FN_BODY)
    files.update(_java("AuthFilter", "import org.springframework.stereotype.Component;\n"
                       "import org.springframework.web.filter.OncePerRequestFilter;",
                       "@Component\npublic class AuthFilter extends OncePerRequestFilter {}"))
    [r] = _eps(analyze_files(files))
    assert (r.endpoint, r.guards) == ("/products", None)
    mvc = _java("Fn", FN_IMPORTS, FN_BODY) | {k: v for k, v in files.items() if "AuthFilter" in k}
    [r] = _eps(analyze_files(mvc))
    assert r.guards == ["AuthFilter"]


SECURED = WEB + "\nimport org.springframework.security.access.prepost.PreAuthorize;"


def _secured(analyze_files, class_ann: str, method_ann: str):
    st = analyze_files(_java("C", SECURED, f"""@RestController
{class_ann}
public class C {{
  @GetMapping("/x")
  {method_ann}
  public String x() {{ return ""; }}
}}"""))
    [r] = _eps(st)
    return r.guards, r.authRequired


def test_permit_all_expression_is_no_guard(analyze_files) -> None:
    assert _secured(analyze_files, "", '@PreAuthorize("permitAll()")') == (None, None)


def test_method_pre_authorize_replaces_the_class_one(analyze_files) -> None:
    assert _secured(analyze_files, '@PreAuthorize("hasRole(\'ADMIN\')")',
                    '@PreAuthorize("permitAll()")') == (None, None)
    assert _secured(analyze_files, '@PreAuthorize("isAuthenticated()")',
                    '@PreAuthorize("hasRole(\'ADMIN\')")') == (
        ["PreAuthorize(hasRole('ADMIN'))"], True)


def test_only_a_signed_in_check_asserts_auth(analyze_files) -> None:
    for expr, required in (("hasAuthority('orders:read')", True),
                           ("isAuthenticated() and #id == principal.id", True),
                           ("hasRole('ADMIN') or isAnonymous()", None),
                           ("!isAuthenticated()", None),
                           ("isAnonymous()", None),
                           ("@acl.canRead(#id)", None)):
        guards, auth = _secured(analyze_files, "", f'@PreAuthorize("{expr}")')
        assert guards == [f"PreAuthorize({expr})"] and auth is required, expr


def test_secured_anonymously_makes_no_claim(analyze_files) -> None:
    assert _secured(analyze_files, "", '@Secured("ROLE_ADMIN")') == (["Secured(ROLE_ADMIN)"], True)
    assert _secured(analyze_files, "", '@Secured("IS_AUTHENTICATED_ANONYMOUSLY")')[1] is None
