# WordPress — entry-point roles

| Construct | Role | Record | Why |
|---|---|---|---|
| `add_action(hook, handler)` | Binding | `eventbus_consumer`, `CONSUMER`, hook endpoint | WordPress invokes the registered callback |
| `add_filter(hook, handler)` | Binding | `eventbus_consumer`, `CONSUMER`, hook endpoint | WordPress invokes the registered filter callback |
| Dynamic hook name or unresolved callback | Binding | consumer with empty endpoint/handler fields | Preserve only syntax that can be verified |
