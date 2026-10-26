# gRPC (Go) — entry-point roles

Parser: `src/breezeai_cog/parsers/go_grpc/` (additive detector + `grpc.services` fact, Go files) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_grpc_go.py`

**Where the RPCs come from.** The generated `*_grpc.pb.go` file (protoc-gen-go-grpc) declares a
`grpc.ServiceDesc` — the service name and every unary method and stream — and a
`RegisterXServer` function that registers it. The `grpc.services` fact reads both; the
`.proto` file itself is not parsed. The generated files are kept by the Go ignore defaults for
this reason.

| Construct | Role | Record | Why |
|---|---|---|---|
| `pb.RegisterAuthServer(server, impl)` — a generated register function of the repo | Binding per RPC | one `route` per method and stream of the service, `method: RPC`, `routeKind: rpc`, `framework: grpc`, `endpoint` = `<ServiceName>/<Method>` with the service name as the descriptor declares it (`auth.Auth/Login`), `handler` = `impl.<Method>` | Each RPC is answered by the implementation's method |
| `impl` is `&server{…}`, a parameter, or `x := NewServer(…)` / `x, err := pkg.New(…)` | Binding | `handlerLine` + `handlerPath` of the method on that type (constructor result types from `go.declarations`) | The type is known |
| `impl` of unknown type | Binding | located when exactly one method of that name exists in the repo, else `handlerLine` empty | Never a guessed declaration |
| `s.RegisterService(&Desc, srv)` inside the generated register function | None | — | Generated plumbing; the binding is where the repo calls `RegisterXServer` |
| `reflection.Register(s)` / `grpc.NewServer(…)` / `s.Serve(lis)` | None | — | Server set-up, not an RPC |
| `grpc.NewServer(grpc.UnaryInterceptor(f), grpc.ChainUnaryInterceptor(a, b), …)` (also `Stream…`) — the server inline or assigned earlier in the function | Guard | their names in `guards` of every RPC registered on that server | Run before every RPC |
| A register function without its generated file in the repo (a module dependency) | Binding | **not recorded** — the methods are unknown | The service is defined outside the repo |
