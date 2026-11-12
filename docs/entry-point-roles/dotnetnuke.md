# DotNetNuke — entry-point roles

Parser: `src/breezeai_cog/parsers/csharp_dnn/` (additive C# detector) — Rules: [entry-point contract](../architecture/entry-points.md) — Tests: `tests/unit/test_roles_dotnetnuke.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `DnnApiController` public action | Binding | `route`, HTTP attribute or name-prefix verb or empty method, `/DesktopModules/{module}/API/{controller}/{action}` | DNN dispatches the action through its Web API route (empty endpoint when module prefix is unresolved) |
| `[ActionName("x")]` on an action | Address modifier | Replaces `{action}` in the endpoint path | DNN routes use the ActionName when present, not the method name |
| `[DnnAuthorize]` | Guard | `guards: ["DnnAuthorize"]`, `authRequired: true` | Requires an authenticated DNN caller |
| `[DnnModuleAuthorize]` | Guard | `guards: ["DnnModuleAuthorize"]`, `authRequired` unset | Default View access level allows anonymous users on public pages — no claim |
| `[AllowAnonymous]` | Guard override | `guards: ["AllowAnonymous"]`, `authRequired` unset | Makes no guarantee (can be overridden) — no claim per extend-capture §3B |
| `[ValidateAntiForgeryToken]` | Guard | Included in `guards` | DNN validates the anti-forgery token before the action |
| `PortalModuleBase` control named by `.dnn` `moduleControl` | Not recorded | No record | DNN serves nothing at a static `/DesktopModules/{module}/{key}` URL; the control's role is undecided |
| `IServiceRouteMapper.RegisterRoutes` | Wiring | No record | It supplies DNN's route convention for controller actions |
