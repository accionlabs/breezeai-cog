# Laravel — entry-point roles

Parser: `src/breezeai_cog/parsers/php_laravel/` (routes) · `php_laravel/events.py` (additive) ·
Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_laravel.py`

A handler is written as the method name with `handlerPath` / `handlerLine` when its declaration
is found (`php/handlers.py`), else as registered (`UserController@index`).

| Construct | Role | Record | Why |
|---|---|---|---|
| `Route::get/post/put/patch/delete/options/any(path, handler)` | Binding | `route`, the verb (`any` → `ANY`), `routeKind: route`, full path, the handler | One handler, inbound API |
| `Route::match(['get', 'post'], path, handler)` | Binding | one `route` per verb | One handler per verb |
| Inline closure handler | Binding | empty `handler`, `handlerLine` = where the closure starts | Anonymous handler |
| A path that is not a literal (`Route::get($uri, …)`) | Binding | `route`, empty `endpoint` | The address can't be resolved (contract §8) |
| `Route::view(path, view)` / `Route::redirect(path, target)` | Binding | `route`, `GET`, empty `handler` | Laravel serves it; a view name / URL is not a handler |
| `Route::fallback(handler)` | Binding | `route`, `ANY`, empty `endpoint` | Catches every unmatched path |
| `Route::resource / apiResource(name, Controller::class)` | Wiring that makes one binding per action | one `route` per action Laravel registers, `/photos/{photo}` (`->only` / `->except` / `->parameters` / nested `a.b` applied). A non-literal `only`/`except`, or `->shallow()`: no records. A name the inflector is unsure of: member routes with an empty `endpoint` | Expands to many handlers |
| `Route::singleton / apiSingleton(name, Controller::class)` | Wiring that makes one binding per action | `show` / `edit` / `update` (api: `show` / `update`); `->creatable()` / `->destroyable()` add the rest | Expands to many handlers |
| `Route::prefix(p)->group(…)` / `Route::group(['prefix' => p], …)` | Wiring | no record; a literal prefix is joined into the nested paths, a dynamic one leaves them empty | Applies to many handlers |
| `->middleware(…)` on a group or route (before or after the verb), `['middleware' => …]` | Guard | names in `guards`, outermost first | Runs before many handlers |
| `Event::listen(name, listener)` | Binding | `eventbus_consumer`, `CONSUMER`, endpoint = the literal name; a listener class is its `handle` method | Inbound event consumer |
| `Event::dispatch(name)` / `event(name)` · `Bus::dispatch(job)` / `dispatch(job)` | Outbound | `eventbus_publish` · `eventbus_send`, no `method`; endpoint = the literal event name | Our code emits |
| `Event::subscribe(Subscriber::class)` | Wiring | no record | A subscriber registers many handlers |
