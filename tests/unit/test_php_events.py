"""Laravel / Symfony event buses run additively, whichever parser owns the file."""

from __future__ import annotations


def _by_type(statements, semantic):
    return [s for s in statements if s.semanticType == semantic]


def test_slim_file_using_symfony_dispatcher_keeps_routes_and_events(analyze_files) -> None:
    stmts = analyze_files({"public/index.php": (
        "<?php\nuse Slim\\Factory\\AppFactory;\n"
        "use Symfony\\Component\\EventDispatcher\\EventDispatcher;\n"
        "$app = AppFactory::create();\n$dispatcher = new EventDispatcher();\n"
        "$app->post('/orders', function ($rq, $rs) use ($dispatcher) {\n"
        "    $dispatcher->dispatch(new OrderPlaced(), 'order.placed');\n"
        "    return $rs;\n});\n"
    )})
    assert [(s.method, s.endpoint, s.framework) for s in _by_type(stmts, "route")] == [
        ("POST", "/orders", "slim")]
    assert [(s.endpoint, s.framework, s.method) for s in _by_type(stmts, "eventbus_publish")] == [
        ("order.placed", "symfony", None)]


def test_component_user_is_not_labelled_symfony(tmp_path) -> None:
    from breezeai_cog import iter_file_records

    f = tmp_path / "modules/custom/src/Sub.php"
    f.parent.mkdir(parents=True)
    f.write_text(
        "<?php\nnamespace Drupal\\custom;\nuse Symfony\\Component\\EventDispatcher\\EventDispatcherInterface;\n"
        "class Sub { function f(EventDispatcherInterface $dispatcher) {\n"
        "    $dispatcher->addListener('kernel.request', [$this, 'onRequest']);\n} }\n"
    )
    (record,) = list(iter_file_records(tmp_path))
    assert record.framework is None
    (consumer,) = _by_type(record.statements, "eventbus_consumer")
    assert (consumer.endpoint, consumer.handler, consumer.framework) == (
        "kernel.request", "Sub@onRequest", "symfony")


def test_laravel_listeners_payloads_and_subscribers(analyze_files) -> None:
    stmts = analyze_files({
        "app/Providers/EventServiceProvider.php": (
            "<?php\nnamespace App\\Providers;\nuse Illuminate\\Support\\Facades\\Event;\n"
            "use App\\Listeners\\SendMail;\n"
            "class EventServiceProvider {\n    public function boot() {\n"
            "        Event::listen('order.shipped', SendMail::class);\n"
            "        Event::listen('order.paid', function ($e) {});\n"
            "        Event::subscribe(UserSubscriber::class);\n"
            "        Event::dispatch($name, 'payload-string');\n"
            "    }\n}\n"
        ),
        "app/Listeners/SendMail.php": (
            "<?php\nnamespace App\\Listeners;\n\nclass SendMail\n{\n    public function handle($e) {}\n}\n"
        ),
    })
    consumers = {s.endpoint: s for s in _by_type(stmts, "eventbus_consumer")}
    assert set(consumers) == {"order.shipped", "order.paid"}  # subscribe() is wiring: no record
    shipped = consumers["order.shipped"]
    assert (shipped.handler, shipped.handlerPath, shipped.handlerLine) == (
        "handle", "app/Listeners/SendMail.php", 6)
    assert (consumers["order.paid"].handler, consumers["order.paid"].handlerLine) == (None, 8)
    # The event name argument is a variable; the payload string is not the address.
    assert [s.endpoint for s in _by_type(stmts, "eventbus_publish")] == [None]
