# DotNetNuke — entry-point roles

Parser: `src/breezeai_cog/parsers/csharp_webforms/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_dotnetnuke.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `DnnApiController` public action | Binding | `route`, HTTP attribute or `GET`, `/DesktopModules/{module}/API/{controller}/{action}` | DNN dispatches the action through its Web API route |
| `[DnnAuthorize]` | Guard | `guards: ["DnnAuthorize"]`, `authRequired: true` | Requires an authenticated DNN caller |
| `[DnnModuleAuthorize]` | Guard | `guards: ["DnnModuleAuthorize"]`, `authRequired: true` | Requires the configured DNN module access level |
| `[AllowAnonymous]` | Guard override | `guards: ["AllowAnonymous"]`, `authRequired: false` | Explicitly allows anonymous access |
| `[ValidateAntiForgeryToken]` | Guard | Included in `guards` | DNN validates the anti-forgery token before the action |
| `PortalModuleBase` control named by `.dnn` `moduleControl` | Binding | `route`, `routeKind: mount`, `ANY`, `/DesktopModules/{module}/{controlKey}` | The manifest mounts the control without a source-code URL |
| `IServiceRouteMapper.RegisterRoutes` | Wiring | No record | It supplies DNN's route convention for controller actions |
