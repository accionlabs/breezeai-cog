# ASP.NET Web Forms — entry-point roles

Parsers: `src/breezeai_cog/parsers/csharp_webforms/` (C#, and the markup), `src/breezeai_cog/parsers/vb_webforms/` (VB code-behind); shared rules in `src/breezeai_cog/parsers/webforms_common/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_webforms.py`, `tests/unit/test_vb_webforms.py`

A VB code-behind (`.aspx.vb`, `.ascx.vb`, `.master.vb`) gets the same records as a C# one, from the
rows below up to `Response.Redirect`; `<WebMethod()> Public Shared Function` is the page method, and
`MapPageRoute` is read from the VB files (a page takes the friendly URLs registered in its own
language). `.ashx` handlers and `web.config` handler mappings are C# only.

| Construct | Role | Record | Why |
|---|---|---|---|
| `.aspx` page (its code-behind `Page` class) | Binding | `route`, `GET`, `routeKind: page`, `/<path>.aspx` (or its `MapPageRoute` URL), `handler` = the page class, `handlerLine` = the class declaration | One page reached by URL; GET and every postback share it (the event handler is chosen by form data) |
| `[WebMethod] public static T M(…)` in a page's code-behind (ASP.NET AJAX page method) | Binding | `route`, `POST`, `routeKind: route`, `endpoint` = the page's endpoint + `/M`, `handler` = `M`, `handlerLine` | Its own URL, one handler |
| `RouteTable.Routes.MapPageRoute(name, url, "~/X.aspx")` | Wiring | statement stays plain; `url` becomes the page's `endpoint` | Gives a page its friendly address |
| `.ascx` user control | None | — | Folded into the pages that host it; no URL of its own |
| `<%@ Register Src="~/X.ascx" %>` / `LoadControl("~/X.ascx")` | Wiring | plain statement, `nodeType: synthetic` (the directive has no tree node), `name: Register`; host → control stays in `importFiles` | Structural link, no handler |
| `<uc:X runat="server" />` registered by `<%@ Register Src TagPrefix TagName %>` | None | plain `element` statement with `handler` = the `TagName`, `handlerPath` = the `.ascx` (only when it exists; a `Namespace`/`Assembly` Register gives none) | Renders the control inside the host page |
| `MasterPageFile="~/Site.master"` | Wiring | statement stays plain; page → master stays in `importFiles` | Structural link (a layout), no handler |
| `.master` page | None | — | A layout, never reached by URL |
| Server-control event (`OnClick="Save_Click"`) | None | plain statement with `handler` = the code-behind method | Runs inside the page's postback |
| `Response.Redirect` / `Server.Transfer` / `NavigateUrl` / `PostBackUrl` to a page | Outbound | `route`, `routeKind: navigation`, `endpoint` = the target page | Our code moving the user to a page |
| `.ashx` generic handler (`IHttpHandler.ProcessRequest`) | Binding | `route`, `/<path>.ashx`, `handler: ProcessRequest` | One handler reached by URL |
| `web.config` `<add verb path type>` under `httpHandlers` / `handlers`, `type` a repo class | Binding | `route` on the class's `ProcessRequest` (`ProcessRequestAsync`, `BeginProcessRequest`, `GetHandler`), `endpoint` = the config's folder under the app root + `<location path>` + `path`, kept as written (`/Admin/Api/*.ashx`); `method` per verb, `*` = `ANY` | The handler answers every request the mapping matches |
| `Global.asax` `Application_Start` | None | — | Start-up |
