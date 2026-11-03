"""gRPC (Go) role table (docs/entry-point-roles/grpc-go.md): one test per row."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

MOD = "module example.com/app\n\ngo 1.22\n"

# Hand-written in the shape protoc-gen-go-grpc generates.
GENERATED = """package authpb

import grpc "google.golang.org/grpc"

type AuthServer interface {
\tLogin(ctx context.Context, in *LoginRequest) (*LoginReply, error)
}

func RegisterAuthServer(s grpc.ServiceRegistrar, srv AuthServer) {
\ts.RegisterService(&Auth_ServiceDesc, srv)
}

var Auth_ServiceDesc = grpc.ServiceDesc{
\tServiceName: "auth.Auth",
\tHandlerType: (*AuthServer)(nil),
\tMethods: []grpc.MethodDesc{
\t\t{
\t\t\tMethodName: "Login",
\t\t\tHandler:    _Auth_Login_Handler,
\t\t},
\t},
\tStreams: []grpc.StreamDesc{
\t\t{
\t\t\tStreamName:    "Watch",
\t\t\tServerStreams: true,
\t\t},
\t},
\tMetadata: "auth.proto",
}
"""

HEAD = ('package main\n\nimport (\n\t"google.golang.org/grpc"\n\t"google.golang.org/grpc/reflection"\n'
        '\t"example.com/app/authpb"\n)\n\n')


def _eps(analyze_files, main: str, **extra: str):
    st = analyze_files({"go.mod": MOD, "authpb/auth_grpc.pb.go": GENERATED, "main.go": HEAD + main, **extra})
    return [s for s in st if is_entry_point(s)]


def test_register_call_is_one_binding_per_rpc(analyze_files) -> None:
    eps = _eps(analyze_files, "type server struct{}\n\nfunc (s *server) Login() {}\nfunc (s *server) Watch() {}\n\n"
                              "func main() {\n\tgs := grpc.NewServer()\n\tauthpb.RegisterAuthServer(gs, &server{})\n}\n")
    got = {(r.semanticType, r.method, r.routeKind, r.framework, r.endpoint, r.handler, r.handlerLine)
           for r in eps}
    assert got == {("route", "RPC", "rpc", "grpc", "auth.Auth/Login", "server.Login", 11),
                   ("route", "RPC", "rpc", "grpc", "auth.Auth/Watch", "server.Watch", 12)}


def test_impl_from_a_constructor_in_another_file(analyze_files) -> None:
    eps = _eps(analyze_files, "func main() {\n\tgs := grpc.NewServer()\n\tapi, err := newAPI()\n\t_ = err\n"
                              "\tauthpb.RegisterAuthServer(gs, api)\n}\n",
               **{"api.go": "package main\n\ntype API struct{}\n\nfunc newAPI() (*API, error) { return &API{}, nil }\n"
                            "func (a *API) Login() {}\nfunc (a *API) Watch() {}\n"})
    assert {(r.handler, r.handlerLine, r.handlerPath) for r in eps} == {
        ("api.Login", 6, "api.go"), ("api.Watch", 7, "api.go")}


def test_impl_of_unknown_type_locates_unique_methods_only(analyze_files) -> None:
    eps = _eps(analyze_files, "type a struct{}\ntype b struct{}\n\nfunc (a) Login() {}\nfunc (b) Login() {}\n"
                              "func (a) Watch() {}\n\n"
                              "func main() {\n\tgs := grpc.NewServer()\n\tauthpb.RegisterAuthServer(gs, impl())\n}\n")
    # `impl()` is not a name: no handler is written for a call argument
    assert {(r.endpoint, r.handler) for r in eps} == {("auth.Auth/Login", None), ("auth.Auth/Watch", None)}
    eps = _eps(analyze_files, "type a struct{}\ntype b struct{}\n\nfunc (a) Login() {}\nfunc (b) Login() {}\n"
                              "func (a) Watch() {}\n\n"
                              "func main() {\n\tgs := grpc.NewServer()\n\tvar srv authpb.AuthServer = pick()\n"
                              "\tauthpb.RegisterAuthServer(gs, srv)\n}\n")
    # `srv`'s type is an interface: Login is declared twice (ambiguous), Watch once
    assert {(r.handler, r.handlerLine) for r in eps} == {("srv.Login", None), ("srv.Watch", 14)}


def test_generated_plumbing_is_not_a_binding(analyze_files) -> None:
    st = analyze_files({"go.mod": MOD, "authpb/auth_grpc.pb.go": GENERATED})
    assert [s for s in st if is_entry_point(s)] == []


def test_server_setup_is_not_a_binding(analyze_files) -> None:
    assert _eps(analyze_files, "func main() {\n\tgs := grpc.NewServer()\n\treflection.Register(gs)\n\tgs.Serve(nil)\n}\n") == []


def test_interceptor_is_a_guard(analyze_files) -> None:
    eps = _eps(analyze_files, "func main() {\n\tgs := grpc.NewServer(grpc.UnaryInterceptor(auth))\n"
                              "\tauthpb.RegisterAuthServer(gs, &server{})\n}\n")
    assert all(r.guards == ["auth"] for r in eps) and eps


def test_register_function_outside_the_repo_is_not_recorded(analyze_files) -> None:
    st = analyze_files({"go.mod": MOD, "main.go": ('package main\n\nimport (\n\t"google.golang.org/grpc"\n'
                                                  '\tpb "github.com/acme/protos/auth"\n)\n\n'
                                                  "func main() {\n\tpb.RegisterAuthServer(grpc.NewServer(), &server{})\n}\n")})
    assert [s for s in st if is_entry_point(s)] == []


def test_interceptors_of_a_server_assigned_earlier(analyze_files) -> None:
    eps = _eps(analyze_files, "func main() {\n\tgs := grpc.NewServer(\n\t\tgrpc.ChainUnaryInterceptor(logging, auth.Check),\n\t)\n"
                              "\tauthpb.RegisterAuthServer(gs, &server{})\n}\n")
    assert {tuple(r.guards) for r in eps} == {("logging", "auth.Check")}
