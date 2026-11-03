# WCF and ASMX — entry-point roles

Parser: `src/breezeai_cog/parsers/csharp_wcf/`, `dotnet_servicehost/` (`.svc` / `.asmx` markup) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_wcf.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `[OperationContract]` method of a `[ServiceContract]` | Binding | `route`, `RPC`, `routeKind: rpc`, `<Contract>/<Operation>`, `handler` = the operation — for an interface contract with one implementing class, that class's method (`Class.Method`) with its `handlerLine` and, in another file, `handlerPath` | One operation, inbound SOAP call |
| `[OperationContract]` + `[WebGet]` / `[WebInvoke]` | Binding | `route`, HTTP verb, `routeKind: route`, `.svc` path joined with the `UriTemplate` (a literal or a `const string`; anything else → empty) — the `.svc` naming the class, or for an interface contract its one implementing class, anywhere in the repo (empty if unknown or ambiguous) | One operation, inbound HTTP call |
| `[OperationContract]` of a service whose `.svc` uses `Factory="…WebScriptServiceHostFactory"` (AJAX) or `WebServiceHostFactory` | Binding | `route`, `POST`, `routeKind: route`, `/<.svc path>/<Operation>` (`[WebGet]` / `[WebInvoke]` on it set the verb and `UriTemplate`) | The host serves every operation over HTTP |
| `[ServiceContract]` on an interface or class | Wiring | — | Marks a container whose operations are bindings |
| Method without `[OperationContract]` | None | — | Not exposed |
| `.svc` `<%@ ServiceHost Service=… %>` | Wiring | statement stays plain; host → code-behind stays in `importFiles` | Gives the service's HTTP operations their address (`[WebGet]` / `[WebInvoke]`, or every operation under a web host factory); no handler of its own |
| `.svc` with `Factory="…DataServiceHostFactory"` (OData) | Wiring | `route`, `routeKind: mount`, `ANY`, `endpoint` `/<.svc path>`, `handler` the service class, `framework: odata` | No code declares an OData service's entity sets, so the host keeps the only record of its address; not an entry point |
| `[WebMethod]` on an ASMX service class | Binding | `route`, `RPC`, `routeKind: rpc`, `<Service>.asmx/<Operation>` | One operation, inbound SOAP call |
| `.asmx` `<%@ WebService Class=… %>` | Wiring | statement stays plain; host → code-behind stays in `importFiles` | Gives the `.asmx` address to its web methods |
| `[GeneratedCode]` client proxy (svcutil) | Outbound | — | Our code calling a service |
