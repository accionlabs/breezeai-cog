# Symfony — entry-point roles

Parser: `src/breezeai_cog/parsers/php_symfony/` (routes) · `php_symfony/events.py` (additive) ·
Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_symfony.py`

A file belongs to Symfony when it uses Symfony's routing or framework bundle. EventDispatcher,
Messenger and HttpFoundation imports alone don't make it Symfony; those components are used by
Laravel, Drupal and Slim apps too.

| Construct | Role | Record | Why |
|---|---|---|---|
| `#[Route(path, methods: [...])]` on a method | Binding | `route`, each declared method (none → `ANY`), `routeKind: route`, `handler` = the method, `handlerLine` = its declaration | One handler, inbound API |
| `#[Route(prefix)]` on the controller class | Wiring | no record; prefix (and name prefix) joined into the methods' routes; on an invokable class, its `__invoke` is the binding | Applies to the class's handlers |
| Localized paths `#[Route(['en' => '/about', 'fr' => '/a-propos'])]` | Binding | one `route` per locale | Each path is its own address |
| `#[IsGranted(...)]` / `#[Security(...)]` on the class or method | Guard | `IsGranted:<role>` in `guards` | Runs before the handler |
| `$routes->add(name, path)->controller(handler)->methods([...])` on a declared `RoutingConfigurator` / `RouteCollection` | Binding | one `route` per method (none → `ANY`); handler like every PHP route handler | The builder call is the binding |
| `->add(…)` on any other receiver | None | — | Not a routing builder |
| `$dispatcher->addListener(name, callback)` | Binding | `eventbus_consumer`, `CONSUMER`, endpoint = the name, handler from the callback — in any PHP file | Inbound event consumer |
| `$dispatcher->dispatch($event, name)` | Outbound | `eventbus_publish`, endpoint = the name argument, else empty | Our code emits |
| Messenger `$bus->dispatch(new Message())` | Outbound | `eventbus_send`, no `method`, empty endpoint | Our code emits |
| `$dispatcher->addSubscriber($s)` | Wiring | no record | A subscriber registers many handlers |
