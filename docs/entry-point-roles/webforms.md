# ASP.NET Web Forms — entry-point roles

Parser: `src/breezeai_cog/parsers/csharp_webforms/` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_webforms.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `.aspx` page (its code-behind `Page` class) | Binding | `route`, `GET`, `routeKind: page`, `/<path>.aspx` (or its `MapPageRoute` URL), `handler` = the page class, `handlerLine` = the class declaration | One page reached by URL; GET and every postback share it (the event handler is chosen by form data) |
| `[WebMethod] public static T M(…)` in a page's code-behind (ASP.NET AJAX page method) | Binding | `route`, `POST`, `routeKind: route`, `endpoint` = the page's endpoint + `/M`, `handler` = `M`, `handlerLine` | Its own URL, one handler |
| `RouteTable.Routes.MapPageRoute(name, url, "~/X.aspx")` | Wiring | statement stays plain; `url` becomes the page's `endpoint` | Gives a page its friendly address |
| `.ascx` user control | None | — | Folded into the pages that host it; no URL of its own |
| `<%@ Register Src="~/X.ascx" %>` / `LoadControl("~/X.ascx")` | Wiring | statement stays plain; host → control stays in `importFiles` | Structural link, no handler |
| `MasterPageFile="~/Site.master"` | Wiring | statement stays plain; page → master stays in `importFiles` | Structural link (a layout), no handler |
| `.master` page | None | — | A layout, never reached by URL |
| Server-control event (`OnClick="Save_Click"`) | None | plain statement with `handler` = the code-behind method | Runs inside the page's postback |
| `Response.Redirect` / `Server.Transfer` / `NavigateUrl` / `PostBackUrl` to a page | Outbound | `navigation`, `endpoint` = the target page | Our code moving the user to a page |
| `.ashx` generic handler (`IHttpHandler.ProcessRequest`) | Binding | `route`, `/<path>.ashx`, `handler: ProcessRequest` | One handler reached by URL |
| `Global.asax` `Application_Start` | None | — | Start-up |
