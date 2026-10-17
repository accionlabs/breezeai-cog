# ASP.NET (MVC, Web API, minimal APIs, Razor Pages) — entry-point roles

Parser: `src/breezeai_cog/parsers/csharp_aspnet/`, `vb_aspnet/` (shared detectors in `aspnet_common.py`), `razor/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_aspnet.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `[HttpGet]` / `[HttpPost]` / … (`[Route]` on the action) on a controller action | Binding | `route`, HTTP verb, `routeKind: route`, path with the controller prefix, `handler` = the action | One handler, inbound API |
| Public action of an MVC `Controller` with no verb attribute (conventional routing) | Binding | `route`, `GET`, `/{Controller}/{Action}` | One handler, reached by the convention route |
| `[Route]` / `[RoutePrefix]` on a controller class (own or inherited) | Wiring | statement stays plain; prefix joins each action's `endpoint` | Points at many actions |
| `routes.MapRoute(...)` / `MapHttpRoute(...)` convention template | Wiring | `route`, `ANY`, `endpoint` = the template, on the `MapRoute(…)` statement itself — the Target Spec's current form. `handler` = the default action when the template fixes it (`{*slug}` with `PageContent` / `Index` → `Index` in `PageContentController`, with its `handlerPath` / `handlerLine`; `PageContent.Index` when that controller isn't found); empty when the template has `{controller}` or `{action}`, since the URL picks the handler and the defaults only fill an absent segment | A template for many actions; no handler of its own |
| `app.MapControllers()` / `MapControllerRoute(...)` | Wiring | — | Enables routing; no handler |
| `app.MapGet(path, handler)` / `MapPost` / … (minimal API) | Binding | `route`, HTTP verb, path; named handler → `handler` + `handlerLine`; inline lambda → `handler` empty, `handlerLine` = the lambda | One handler, inbound API |
| `app.MapGroup(prefix)` | Wiring | statement stays plain; prefix joins the group's endpoints | Points at many endpoints |
| `[Authorize]` / `[AllowAnonymous]` on a controller or action | Guard | name in `guards` | Runs before the handler; applies to many |
| `app.UseAuthentication()` / `UseAuthorization()` / `UseMiddleware<T>()` / MVC filters | Guard | — | Run before many handlers |
| `app.MapHealthChecks(path)` | Binding | `route`, `GET`, `endpoint` = path, `handler` empty | A real URL outside callers hit; the framework's handler runs, none of ours |
| `MapGraphQL()` / `UseGraphQL<T>()` | Wiring | `route`, `POST`, `endpoint` = the path (else `/graphql`), `framework: graphql`, no handler — the Target Spec's current form | Transport path for many GraphQL operations |
| `UseGraphQLPlayground()` / `Voyager` / `GraphiQL` / `Altair` | Binding | `route`, `GET`, `endpoint` = the UI path, `handler` empty | A real URL; the framework serves it, no handler of ours |
| `@page "/path"` in a `.cshtml` / `.razor` file | Binding | `route`, `routeKind: page`, path, `handler` = the `@model` PageModel class (`.cshtml`, declared in another file; not located yet: no `handlerLine` / `handlerPath`) or the component named by the file (`.razor`, `handlerLine` 1: the component is the whole file) | One page reached by URL |
| `RecurringJob.AddOrUpdate(...)` (Hangfire) | Binding | `timer`, cron as `endpoint`, `handler` = the job method | One handler, started by a schedule |
| `HttpClient` calls | Outbound | `api_call` | Our code calling out |
| `WebApplication.CreateBuilder(...)` / `app.Run()` / `Program.Main` | None | — | Start-up |
