# WSDL files — entry-point roles

Parser: `src/breezeai_cog/parsers/wsdl/` (`.wsdl`, WSDL 1.1) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_wsdl.py`

A WSDL declares a SOAP service's operations and messages, but names no handler: the operation's
entry point is the code that implements it (a WCF `[OperationContract]`, an ASMX `[WebMethod]`).
So, like a GraphQL SDL root field, a WSDL operation is captured as a plain statement and is
never an entry point. The parser cannot tell a service's own published contract from the WSDL of
a remote service the code only calls; recording neither as an entry point is the role that
records less (contract §8).

| Construct | Role | Record | Why |
|---|---|---|---|
| `portType/operation` | Wiring | plain statement: `endpoint` = `<PortType>/<Operation>`, `requestDTO` / `responseDTO` = the input / output message names, `framework: wsdl`; the implementing operation in code is the binding | Declares the operation's name and messages; names no handler |
| `portType/operation` next to its implementation in the repo (e.g. a WCF `[ServiceContract]`) | Wiring | as above — the implementation's `route` is the only entry point | One operation, one record |
| `binding` and its `operation` children (`soap:binding`, `soap12:binding`) | None | — | Wire protocol for a `portType`'s operations |
| `service` / `port` (`soap:address location`) | None | — | Deployment address; not modeled |
| `message` | None | `data_model` (not an entry point) | Declares a message body |
| `import` | None | plain statement: `name` = namespace or location, no `endpoint`; the location is an `importFiles` (in-repo) or `externalImports` (URL) entry | References another document (a structural link, not a binding) |
