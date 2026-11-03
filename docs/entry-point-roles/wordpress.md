# WordPress — entry-point roles

Detector: `src/breezeai_cog/parsers/php/wordpress.py` (additive — runs on every PHP file) ·
Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_wordpress.py`

A hook callback is written as registered and led to its declaration after parsing
(`php/handlers.py`): a function declared once in the file, or `Class@method` through the
`php.method-lines` fact.

| Construct | Role | Record | Why |
|---|---|---|---|
| `add_action(hook, 'my_fn')` / `add_filter(hook, 'my_fn')` | Binding | `eventbus_consumer`, `CONSUMER`, endpoint = the hook, `handler` = the function, `handlerLine` = its declaration when in this file | WordPress invokes the callback |
| `[$this, 'm']` / `[self::class, 'm']` / `[Cls::class, 'm']` / `'Cls::m'` | Binding | handler `m` of the enclosing / named class | The class is in the syntax |
| `[$object, 'm']` (class not in the syntax) | Binding | empty `handler` | A bare `m` would be read as an unrelated global function |
| Inline closure callback | Binding | empty `handler`, `handlerLine` = where the closure starts | Anonymous handler |
| A hook name that is not a literal | Binding | empty `endpoint` | The address can't be resolved (contract §8) |
