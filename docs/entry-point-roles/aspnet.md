# ASP.NET (MVC, Web API, minimal APIs, Razor Pages) — entry-point roles

Parser: `src/breezeai_cog/parsers/csharp_aspnet/`, `vb_aspnet/` (shared detectors in `aspnet_common.py`), `razor/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_aspnet.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `[HttpGet]` / `[HttpPost]` / … (`[Route]` on the action) on a controller action | Binding | `route`, HTTP verb, `routeKind: route`, path with the controller prefix, `handler` = the action | One handler, inbound API |
| Public action of an MVC `Controller` with no verb attribute (conventional routing) | Binding | `route`, `GET`, `/{Controller}/{Action}` | One handler, reached by the convention route |
| `[Route]` / `[RoutePrefix]` on a controller class (own or inherited) | Wiring | statement stays plain; prefix joins each action's `endpoint` | Points at many actions |
| `routes.MapRoute(...)` / `MapHttpRoute(...)` convention template | Wiring | `route`, `ANY`, `endpoint` = the template, on the `MapRoute(…)` statement itself — the Target Spec's current form. `handler` = the default action when the template fixes it (`{*slug}` with `PageContent` / `Index` → `Index` in `PageContentController`, with its `handlerPath` / `handlerLine`; `PageContent.Index` when that controller isn't found); empty when the template has `{controller}` or `{action}`, since the URL picks the handler and the defaults only fill an absent segment | A template for many actions; no handler of its own |
| `[RouteArea("X")]` / `[RouteArea(X, AreaPrefix = "p")]` on a controller (MVC 5) | Wiring | — (the `AreaPrefix`, else the area name, prefixes the controller's attribute routes; a constant argument is resolved; unresolved → those addresses empty) | Adds part of an address |
| `AreaRegistration` with `context.MapRoute(…, "Tests/{controller}/{action}/{id}", …, namespaces)` (MVC 5) | Wiring | — (the convention address of an action of a controller in those namespaces — default: the registration's own and below — is the template: `/Tests/Static/Index`) | Adds part of an address |
| `[Area("Admin")]` on a controller (ASP.NET Core) | Wiring | — (fills the `[area]` token of its attribute templates) | Adds part of an address |
| A controller under an `Areas/` folder whose area nothing in code names | — | its convention routes have an empty `endpoint` (never the folder as a prefix, never `/{Controller}/{Action}` as if outside an area); attribute routes as written | The address is unknown |
| `app.MapControllers()` / `MapControllerRoute(...)` | Wiring | — | Enables routing; no handler |
| `app.MapGet(path, handler)` / `MapPost` / … (minimal API) | Binding | `route`, HTTP verb, path; named handler → `handler` + `handlerLine`; inline lambda → `handler` empty, `handlerLine` = the lambda | One handler, inbound API |
| `app.MapGroup(prefix)` | Wiring | statement stays plain; prefix joins the group's endpoints | Points at many endpoints |
| `[Authorize]` / `[AllowAnonymous]` on a controller or action | Guard | name in `guards` | Runs before the handler; applies to many |
| `app.UseAuthentication()` / `UseAuthorization()` / `UseMiddleware<T>()` / MVC filters | Guard | — | Run before many handlers |
| `app.MapHealthChecks(path)` | Binding | `route`, `GET`, `endpoint` = path, `handler` empty | A real URL outside callers hit; the framework's handler runs, none of ours |
| `MapGraphQL()` / `UseGraphQL<T>()` | Wiring | `route`, `POST`, `endpoint` = the path (else `/graphql`), `framework: graphql`, no handler — the Target Spec's current form | Transport path for many GraphQL operations |
| `UseGraphQLPlayground()` / `Voyager` / `GraphiQL` / `Altair` | Binding | `route`, `GET`, `endpoint` = the UI path, `handler` empty | A real URL; the framework serves it, no handler of ours |
| `@page "/path"` in a `.cshtml` / `.razor` file | Binding | `route`, `routeKind: page`, path, `handler` = the `@model` PageModel class as written (`.cshtml`; `handlerPath` / `handlerLine` = its declaration in the page's code-behind `<page>.cshtml.cs`, a qualified name matching its namespace; not there or declared twice → no location; a generic `@model List<T>` → no handler) or the component named by the file (`.razor`, `handlerLine` 1: the component is the whole file) | One page reached by URL |
| `RecurringJob.AddOrUpdate(...)` (Hangfire) | Binding | `timer`, cron as `endpoint`, `handler` = the job method | One handler, started by a schedule |
| `HttpClient` calls | Outbound | `api_call` | Our code calling out |
| `WebApplication.CreateBuilder(...)` / `app.Run()` / `Program.Main` | None | — | Start-up |
