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


def test_scheduled_interval_with_a_time_unit_is_in_milliseconds(analyze_files) -> None:
    st = analyze_files(_java(
        "J",
        "import org.springframework.scheduling.annotation.Scheduled;\n"
        "import java.util.concurrent.TimeUnit;",
        """public class J {
  @Scheduled(fixedRate = 5, timeUnit = TimeUnit.SECONDS)
  public void a() {}
  @Scheduled(fixedDelayString = "2", timeUnit = TimeUnit.MINUTES)
  public void b() {}
  @Scheduled(fixedRate = 5, timeUnit = unit)
  public void c() {}
  @Scheduled(fixedRate = 5, timeUnit = TimeUnit.MICROSECONDS)
  public void d() {}
  @Scheduled(cron = "0 * * * *", timeUnit = TimeUnit.SECONDS)
  public void e() {}
}""",
    ))
    got = {r.handler: r.endpoint for r in _eps(st)}
    assert got == {
        "a": "5000", "b": "120000",
        "c": None,            # a unit that is not a literal TimeUnit: unknown
        "d": None,            # not a whole number of milliseconds
        "e": "0 * * * *",     # timeUnit does not apply to cron
    }


def test_scheduled_inside_a_schedules_container_is_binding(analyze_files) -> None:
    st = analyze_files(_java(
        "J",
        "import org.springframework.scheduling.annotation.Scheduled;\n"
        "import org.springframework.scheduling.annotation.Schedules;",
        """public class J {
  @Schedules({ @Scheduled(cron = "0 0 2 * * *"), @Scheduled(fixedRate = 60000) })
  public void cleanup() {}
  @Schedules(@Scheduled(fixedDelay = 5))
  public void single() {}
  @Schedules(value = { @Scheduled(cron = "0 0 4 * * *") })
  public void named() {}
  @Scheduled(cron = "0 0 3 * * *")
  @Scheduled(fixedDelay = 5000)
  public void twice() {}
}""",
    ))
    got = sorted((r.handler, r.endpoint, r.method, r.framework) for r in _eps(st))
    assert got == [
        ("cleanup", "0 0 2 * * *", "PERIODIC", "spring"),
        ("cleanup", "60000", "PERIODIC", "spring"),
        ("named", "0 0 4 * * *", "PERIODIC", "spring"),
        ("single", "5", "PERIODIC", "spring"),
        ("twice", "0 0 3 * * *", "PERIODIC", "spring"),
        ("twice", "5000", "PERIODIC", "spring"),
    ]


SCHEDULER = """import org.springframework.scheduling.TaskScheduler;
import org.springframework.scheduling.config.ScheduledTaskRegistrar;
import org.springframework.scheduling.annotation.SchedulingConfigurer;
import java.time.Duration;"""


def test_task_scheduler_and_registrar_calls_are_bindings(analyze_files) -> None:
    st = analyze_files(_java(
        "J",
        SCHEDULER,
        """public class J implements SchedulingConfigurer {
  private TaskScheduler scheduler;
  public void start() { scheduler.scheduleAtFixedRate(this::poll, Duration.ofSeconds(30)); }
  public void again() { this.scheduler.scheduleWithFixedDelay(this::poll, 2_000L); }
  public void configureTasks(ScheduledTaskRegistrar r) {
    r.addFixedRateTask(this::report, 10000);
    r.addCronTask(this::nightly, "0 0 2 * * *");
  }
  void poll() {}
  void report() {}
  void nightly() {}
}""",
    ))
    got = sorted((r.handler, r.endpoint) for r in _eps(st))
    assert got == [("nightly", "0 0 2 * * *"), ("poll", "2000"), ("poll", "30000"),
                   ("report", "10000")]
    assert {(r.framework, r.method) for r in _eps(st)} == {("spring", "PERIODIC")}


def test_jdk_scheduled_executor_is_not_a_spring_timer(analyze_files) -> None:
    st = analyze_files(_java(
        "J",
        "import java.util.concurrent.ScheduledExecutorService;\n"
        "import java.util.concurrent.TimeUnit;\n"
        "import org.springframework.stereotype.Component;",
        """@Component
public class J {
  private ScheduledExecutorService exec;
  public void start() { exec.scheduleAtFixedRate(this::poll, 0, 30, TimeUnit.SECONDS); }
  void poll() {}
}""",
    ))
    assert _eps(st) == []


def test_scheduler_period_that_is_not_a_literal_is_empty(analyze_files) -> None:
    st = analyze_files(_java(
        "J",
        SCHEDULER,
        """public class J {
  private TaskScheduler scheduler;
  static final long EVERY_MS = 10000;
  public void start(Duration every, long seconds) {
    scheduler.scheduleAtFixedRate(this::poll, every);
    scheduler.scheduleAtFixedRate(this::poll, EVERY_MS);
    scheduler.scheduleAtFixedRate(this::poll, Duration.ofSeconds(seconds));
    scheduler.scheduleAtFixedRate(() -> poll(), Duration.ofMillis(500));
  }
  void poll() {}
}""",
    ))
    got = sorted(((r.endpoint or ""), r.handler or "") for r in _eps(st))
    # A runtime period or a numeric constant is unknown → empty; a lambda task has no handler
    # name, only its line.
    assert got == [("", "poll"), ("", "poll"), ("", "poll"), ("500", "")]


def test_scheduler_name_declared_with_two_types_is_not_read(analyze_files) -> None:
    st = analyze_files(_java(
        "J",
        SCHEDULER + "\nimport java.util.concurrent.ScheduledExecutorService;",
        """public class J {
  private TaskScheduler scheduler;
  public void start() { scheduler.scheduleAtFixedRate(this::poll, Duration.ofSeconds(30)); }
  public void other(ScheduledExecutorService scheduler) {
    scheduler.scheduleAtFixedRate(this::poll, 0, 30, java.util.concurrent.TimeUnit.SECONDS);
  }
  void poll() {}
}""",
    ))
    # Which object a `scheduler.…` call uses is unsure: no timer from either call.
    assert _eps(st) == []


def test_registrar_task_object_is_binding(analyze_files) -> None:
    st = analyze_files(_java(
        "J",
        SCHEDULER + "\nimport org.springframework.scheduling.config.*;",
        """public class J implements SchedulingConfigurer {
  public void configureTasks(ScheduledTaskRegistrar registrar) {
    Duration interval = Duration.ofHours(hours());
    registrar.addFixedDelayTask(new FixedDelayTask(this::sync, interval, Duration.ZERO));
    registrar.addFixedRateTask(new FixedRateTask(this::poll, Duration.ofMinutes(5), Duration.ZERO));
    registrar.addCronTask(new CronTask(this::nightly, "0 0 2 * * *"));
  }
  long hours() { return 24; }
  void sync() {}
  void poll() {}
  void nightly() {}
}""",
    ))
    got = sorted((r.handler, r.endpoint or "") for r in _eps(st))
    # The interval held in a variable is unknown → empty, never the variable's name.
    assert got == [("nightly", "0 0 2 * * *"), ("poll", "300000"), ("sync", "")]


# ── Spring Cloud Stream ─────────────────────────────────────────────────────────────────────
STREAM = "import org.springframework.cloud.stream.annotation.StreamListener;"


def test_stream_listener_is_a_consumer_on_its_binding(analyze_files) -> None:
    st = analyze_files(_java("L", STREAM, """public class L {
  static final String PAYMENTS = "payments";
  @StreamListener("orders")
  public void handle(String msg) {}
  @StreamListener(target = PAYMENTS)
  public void pay(String msg) {}
}"""))
    got = sorted((r.semanticType, r.routeKind, r.method, r.endpoint, r.handler, r.framework)
                 for r in _eps(st))
    assert got == [("eventbus_consumer", "message", "EVENT", "orders", "handle", "spring"),
                   ("eventbus_consumer", "message", "EVENT", "payments", "pay", "spring")]


def test_stream_listener_takes_the_configured_destination_of_its_binding(analyze_files) -> None:
    files = _java("L", STREAM, """public class L {
  @StreamListener("input")
  public void a(String msg) {}
  @StreamListener("multi")
  public void b(String msg) {}
  @StreamListener("unset")
  public void c(String msg) {}
}""")
    files["src/main/resources/application.properties"] = (
        "spring.cloud.stream.bindings.input.destination=orders-topic\n"
        "spring.cloud.stream.bindings.multi.destination=t1, t2\n"
        "spring.cloud.stream.bindings.unset.destination=${missing.topic}\n")
    got = sorted((r.handler, r.endpoint or "") for r in _eps(analyze_files(files)))
    # The binding name is only the address when no destination is configured; an unresolved
    # destination is unknown, never the binding name.
    assert got == [("a", "orders-topic"), ("b", "t1"), ("b", "t2"), ("c", "")]


# ── Spring Security URL rules ───────────────────────────────────────────────────────────────
SECURITY = "src/main/java/a/SecurityConfig.java"


def _guarded(analyze_files, configure: str, controller: str) -> dict[tuple[str, str], tuple]:
    files = _java("C", WEB, controller)
    files[SECURITY] = ("package a;\nimport org.springframework.http.HttpMethod;\n"
                       "import org.springframework.security.config.annotation.web.builders.*;\n"
                       f"public class SecurityConfig {{\n{configure}\n}}\n")
    return {(r.method, r.endpoint): (r.guards, r.authRequired)
            for r in _eps(analyze_files(files)) if r.semanticType == "route"}


ADMIN = """@RestController
@RequestMapping("/admin")
public class C {
  @GetMapping("/users") public String users() { return ""; }
  @GetMapping("/public") public String pub() { return ""; }
  @GetMapping("/{id}") public String one() { return ""; }
  @PostMapping("/users") public String create() { return ""; }
}"""


def test_ant_matcher_rule_is_the_guard_of_the_routes_it_matches(analyze_files) -> None:
    got = _guarded(analyze_files, """  protected void configure(HttpSecurity http) throws Exception {
    http.authorizeRequests().antMatchers("/admin/public").permitAll()
        .antMatchers("/admin/**").hasRole("ADMIN");
  }""", ADMIN)
    assert got[("GET", "/admin/users")] == (["hasRole(ADMIN)"], True)
    assert got[("GET", "/admin/public")] == (None, None)  # the first matching rule: permitAll
    # `/admin/{id}` may be `/admin/public`: which rule applies is unsure → no guard.
    assert got[("GET", "/admin/{id}")] == (None, None)


def test_request_matchers_lambda_style_with_a_method_and_any_request(analyze_files) -> None:
    got = _guarded(analyze_files, """  SecurityFilterChain chain(HttpSecurity http) throws Exception {
    http.authorizeHttpRequests(auth -> auth
        .requestMatchers(HttpMethod.POST, "/admin/**").hasAnyRole("ADMIN", "OPS")
        .anyRequest().authenticated());
    return http.build();
  }""", ADMIN)
    assert got[("POST", "/admin/users")] == (["hasAnyRole(ADMIN,OPS)"], True)
    assert got[("GET", "/admin/users")] == (["authenticated()"], True)


def test_rule_that_might_match_first_leaves_no_guard(analyze_files) -> None:
    got = _guarded(analyze_files, """  protected void configure(HttpSecurity http) throws Exception {
    http.authorizeRequests().antMatchers("/admin/new").permitAll()
        .antMatchers("/admin/**").hasRole("ADMIN");
  }""", ADMIN)
    # `/admin/{id}` may be `/admin/new` (open) or not (admin-only): unsure → no guard.
    assert got[("GET", "/admin/{id}")] == (None, None)
    assert got[("GET", "/admin/users")] == (["hasRole(ADMIN)"], True)


def test_route_matched_by_no_rule_or_an_unreadable_one_gets_no_guard(analyze_files) -> None:
    got = _guarded(analyze_files, """  protected void configure(HttpSecurity http) throws Exception {
    http.authorizeRequests().antMatchers("/other/**").hasRole("X")
        .regexMatchers("/adm.*").hasRole("ADMIN")
        .antMatchers("/admin/**").hasRole("ADMIN");
  }""", ADMIN)
    # No rule before the regex matches; the regex might: no guard rather than a guess.
    assert {v for v in got.values()} == {(None, None)}


def test_two_security_chains_in_a_module_give_no_guards(analyze_files) -> None:
    got = _guarded(analyze_files, """  SecurityFilterChain api(HttpSecurity http) throws Exception {
    http.authorizeHttpRequests(a -> a.anyRequest().authenticated());
    return http.build();
  }
  SecurityFilterChain web(HttpSecurity http) throws Exception {
    http.authorizeHttpRequests(a -> a.anyRequest().permitAll());
    return http.build();
  }""", ADMIN)
    # Which chain serves a route depends on bean order: unsure.
    assert {v for v in got.values()} == {(None, None)}


def test_ignored_paths_bypass_security(analyze_files) -> None:
    got = _guarded(analyze_files, """  WebSecurityCustomizer ignore() {
    return web -> web.ignoring().antMatchers("/admin/public");
  }
  protected void configure(HttpSecurity http) throws Exception {
    http.authorizeRequests().anyRequest().authenticated();
  }""", ADMIN)
    assert got[("GET", "/admin/public")] == (None, None)
    assert got[("GET", "/admin/users")] == (["authenticated()"], True)


def test_no_security_config_gives_no_guard(analyze_files) -> None:
    st = analyze_files(_java("C", WEB, ADMIN))
    assert {(r.guards, r.authRequired) for r in _eps(st)} == {(None, None)}
