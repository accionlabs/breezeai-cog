"""Role table: docs/entry-point-roles/symfony.md — one test per row."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

ATTR = "<?php\nnamespace App\\Controller;\nuse Symfony\\Component\\Routing\\Attribute\\Route;\n"


def _eps(analyze_files, files: dict[str, str]):
    return [s for s in analyze_files(files) if is_entry_point(s)]


def test_method_attribute_is_binding(analyze_files) -> None:
    (r,) = _eps(analyze_files, {"src/Controller/Blog.php": ATTR + (
        "class Blog\n{\n    #[Route('/blog', name: 'blog', methods: ['GET'])]\n"
        "    public function list() {}\n}\n")})
    assert (r.method, r.endpoint, r.routeKind, r.framework, r.name) == (
        "GET", "/blog", "route", "symfony", "blog")
    assert (r.handler, r.handlerLine, r.handlerPath) == ("list", 6, None)


def test_class_attribute_is_wiring_and_invokable_binds(analyze_files) -> None:
    eps = _eps(analyze_files, {
        "src/Controller/Admin.php": ATTR + "#[Route('/admin', name: 'admin_')]\nclass Admin\n{\n"
        "    #[Route('/users', name: 'users')]\n    public function users() {}\n}\n",
        "src/Controller/Ping.php": ATTR + "#[Route('/ping', methods: ['GET'])]\nclass Ping\n{\n"
        "    public function __invoke() {}\n}\n",
    })
    assert sorted((r.method, r.endpoint, r.handler, r.name) for r in eps) == [
        ("ANY", "/admin/users", "users", "admin_users"), ("GET", "/ping", "__invoke", None)]


def test_localized_paths_are_one_binding_each(analyze_files) -> None:
    eps = _eps(analyze_files, {"src/Controller/About.php": ATTR + (
        "class About\n{\n    #[Route(['en' => '/about', 'fr' => '/a-propos'])]\n"
        "    public function show() {}\n}\n")})
    assert sorted(r.endpoint for r in eps) == ["/a-propos", "/about"]


def test_is_granted_is_guard(analyze_files) -> None:
    (r,) = _eps(analyze_files, {"src/Controller/Acc.php": ATTR + (
        "use Symfony\\Component\\Security\\Http\\Attribute\\IsGranted;\n"
        "#[IsGranted('ROLE_USER')]\nclass Acc\n{\n    #[Route('/account')]\n"
        "    public function show() {}\n}\n")})
    assert r.guards == ["IsGranted:ROLE_USER"]


BUILDER = (
    "<?php\nuse Symfony\\Component\\Routing\\Loader\\Configurator\\RoutingConfigurator;\n"
    "return function (RoutingConfigurator $routes) {\n{body}};\n"
)


def test_builder_is_binding(analyze_files) -> None:
    eps = _eps(analyze_files, {"config/routes.php": BUILDER.replace("{body}", (
        "    $routes->add('list', '/list')->controller('App\\Controller\\Blog::list')"
        "->methods(['GET', 'HEAD']);\n"))})
    assert sorted((r.method, r.endpoint, r.handler, r.name) for r in eps) == [
        ("GET", "/list", "App\\Controller\\Blog@list", "list"),
        ("HEAD", "/list", "App\\Controller\\Blog@list", "list")]


def test_add_on_other_receivers_is_none(analyze_files) -> None:
    assert _eps(analyze_files, {"config/routes.php": BUILDER.replace(
        "{body}", "    $this->routeParams->add('k', 'v');\n")}) == []


EVENTS = (
    "<?php\nuse Symfony\\Component\\EventDispatcher\\EventDispatcher;\n"
    "use Symfony\\Component\\Messenger\\MessageBusInterface;\n"
    "function wire(EventDispatcher $dispatcher, MessageBusInterface $bus) {\n{body}}\n"
)


def _events(analyze_files, body: str):
    return [s for s in analyze_files({"src/wire.php": EVENTS.replace("{body}", body)})
            if (s.semanticType or "").startswith("eventbus_")]


def test_add_listener_is_consumer_binding(analyze_files) -> None:
    (r,) = _events(analyze_files, "    $dispatcher->addListener('kernel.request', [Listener::class, 'onRequest']);\n")
    assert (r.semanticType, r.method, r.endpoint, r.handler, r.framework) == (
        "eventbus_consumer", "CONSUMER", "kernel.request", "Listener@onRequest", "symfony")


def test_dispatch_is_outbound(analyze_files) -> None:
    (r,) = _events(analyze_files, "    $dispatcher->dispatch(new OrderPlaced(), 'order.placed');\n")
    assert (r.semanticType, r.endpoint, r.method) == ("eventbus_publish", "order.placed", None)


def test_messenger_dispatch_is_send(analyze_files) -> None:
    (r,) = _events(analyze_files, "    $bus->dispatch(new SendInvoice());\n")
    assert (r.semanticType, r.endpoint, r.method) == ("eventbus_send", None, None)


def test_add_subscriber_is_wiring(analyze_files) -> None:
    assert _events(analyze_files, "    $dispatcher->addSubscriber(new Sub());\n") == []
