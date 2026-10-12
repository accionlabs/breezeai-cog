# ASP.NET (MVC, Web API, minimal APIs, Razor Pages) — entry-point roles

Parser: `src/breezeai_cog/parsers/csharp_aspnet/`, `vb_aspnet/` (shared detectors in `aspnet_common.py`), `razor/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_aspnet.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `[HttpGet]` / `[HttpPost]` / … (`[Route]` on the action) on a controller action | Binding | `route`, HTTP verb, `routeKind: route`, path with the controller prefix, `handler` = the action | One handler, inbound API |
| Public action of an MVC `Controller` with no verb attribute (conventional routing) | Binding | `route`, `GET`, `/{Controller}/{Action}` | One handler, reached by the convention route |
| `[Route]` / `[RoutePrefix]` on a controller class (own or inherited) | Wiring | statement stays plain; prefix joins each action's `endpoint` | Points at many actions |
| `routes.MapRoute(...)` / `MapHttpRoute(...)` convention template | Wiring | statement stays plain | A template for many actions; no handler of its own |
| `app.MapControllers()` / `MapControllerRoute(...)` | Wiring | — | Enables routing; no handler |
| `app.MapGet(path, handler)` / `MapPost` / … (minimal API) | Binding | `route`, HTTP verb, path; named handler → `handler` + `handlerLine`; inline lambda → `handler` empty, `handlerLine` = the lambda | One handler, inbound API |
| `app.MapGroup(prefix)` | Wiring | statement stays plain; prefix joins the group's endpoints | Points at many endpoints |
| `[Authorize]` / `[AllowAnonymous]` on a controller or action | Guard | name in `guards` | Runs before the handler; applies to many |
| `app.UseAuthentication()` / `UseAuthorization()` / `UseMiddleware<T>()` / MVC filters | Guard | — | Run before many handlers |
| `app.MapHealthChecks(path)` | None | — | The framework's handler runs; no code of ours |
| `MapGraphQL()` / `UseGraphQL<T>()` | Wiring | statement stays plain | Transport path for many GraphQL operations |
| `UseGraphQLPlayground()` / `Voyager` / `GraphiQL` / `Altair` | None | — | Framework dev UI; no code of ours |
| `@page "/path"` in a `.cshtml` / `.razor` file | Binding | `route`, `routeKind: page`, path, `handler` = the page model or component | One page reached by URL |
| `RecurringJob.AddOrUpdate(...)` (Hangfire) | Binding | `timer`, cron as `endpoint`, `handler` = the job method | One handler, started by a schedule |
| `HttpClient` calls | Outbound | `api_call` | Our code calling out |
| `WebApplication.CreateBuilder(...)` / `app.Run()` / `Program.Main` | None | — | Start-up |
