# JSP — entry-point roles

Parser: `src/breezeai_cog/parsers/jsp/` (`.jsp`, `.jspx`, `.jspf`, `.tag`, `.tagx`; every JSP
version, 1.0 to Jakarta Pages 4.0; template files, so only with `--capture-templates`) · Rules:
[entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_jsp.py`

A JSP's URL is its path from the **web root** — the directory holding `WEB-INF`
(`src/main/webapp/shop/index.jsp` → `/shop/index.jsp`). Struts2 tags (`s:form`, `s:url`, …) are
in the [Struts2](struts2.md) table.

| Construct | Role | Record | Why |
|---|---|---|---|
| A `.jsp` / `.jspx` page inside a web application, outside `WEB-INF` | Binding | `route`, `GET`, `routeKind: page`, `framework: jsp`, `endpoint` = its URL, `handler` = the page's file name, `handlerLine` = 1 (the page is its own handler) — on the page's first line | A page reached by URL; its code is the page itself |
| A page under `WEB-INF` (`/WEB-INF/jsp/login.jsp`) | None | — | No URL reaches it: an action or servlet renders it (that action's result records the link) |
| A page outside any web application (no `WEB-INF` above it) | None | — | Its address cannot be known |
| A fragment (`.jspf`), a tag file (`.tag`, `.tagx`) | None | — (an `importFiles` edge from the pages that include or use it) | Part of another page |
| `<%@ include file %>`, `<jsp:include page>`, `<c:import url>` | None | — (`importFiles` edge to the included file) | Part of this page |
| `<a href>`, `<form action>`, `<jsp:forward page>`, `<c:url value>`, `<c:redirect url>` to a page (`.jsp`, `.jspx`) or an action (`.action`, `.do`, the configured Struts extension) | Outbound | `route`, `routeKind: navigation`, `framework: jsp`, `endpoint` = the target's URL (`${pageContext.request.contextPath}` / `<%= request.getContextPath() %>` dropped; a relative target resolved against the page's URL); a dynamic or unresolvable target, an external URL or another file type → no record | Our page sending the user on |
| A page in a fixture folder (`fixtures/`, `mocks/`, …) | None | — | A test stand-in |
