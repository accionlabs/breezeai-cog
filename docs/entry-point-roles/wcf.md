# WCF and ASMX — entry-point roles

Parser: `src/breezeai_cog/parsers/csharp_wcf/`, `dotnet_servicehost/` (`.svc` / `.asmx` markup) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_wcf.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `[OperationContract]` method of a `[ServiceContract]` | Binding | `route`, `RPC`, `routeKind: rpc`, `<Contract>/<Operation>`, `handler` = the operation | One operation, inbound SOAP call |
| `[OperationContract]` + `[WebGet]` / `[WebInvoke]` | Binding | `route`, HTTP verb, `routeKind: route`, `.svc` path joined with the `UriTemplate` — the `.svc` naming the class, or for an interface contract its one implementing class, anywhere in the repo (empty if unknown or ambiguous) | One operation, inbound HTTP call |
| `[ServiceContract]` on an interface or class | Wiring | — | Marks a container whose operations are bindings |
| Method without `[OperationContract]` | None | — | Not exposed |
| `.svc` `<%@ ServiceHost Service=… %>` | Wiring | `route`, `RPC`, `routeKind: rpc`, `endpoint` = the `.svc` file, `handler` = the service class — the Target Spec's current form; host → code-behind also in `importFiles` | Gives every operation of the service its address; no handler of its own |
| `[WebMethod]` on an ASMX service class | Binding | `route`, `RPC`, `routeKind: rpc`, `<Service>.asmx/<Operation>` | One operation, inbound SOAP call |
| `.asmx` `<%@ WebService Class=… %>` | Wiring | `route`, `RPC`, `routeKind: rpc`, `endpoint` = the `.asmx` file, `handler` = the class — the Target Spec's current form; host → code-behind also in `importFiles` | Gives the `.asmx` address to its web methods |
| `[GeneratedCode]` client proxy (svcutil) | Outbound | — | Our code calling a service |
