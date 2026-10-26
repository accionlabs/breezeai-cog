# Entry-point role tables

One table per parser that records entry points. Each table gives every framework construct the
parser knows one role — Binding, Wiring, Guard, Outbound or None — and the record the
[entry-point contract](../architecture/entry-points.md) requires for it. The rows are the
contract applied to one framework; they say what the parser must do, not what it does today.

Each table has a test file, `tests/unit/test_roles_<framework>.py`, with one test per row. A row
the parser does not meet yet is marked `xfail(strict=True)` with the reason, so fixing the parser
flips the test and the marker must be removed.

When you add a framework construct to a parser, add its row here and its test.

| Language | Tables |
|---|---|
| TypeScript / JavaScript — servers | [Express](express.md) · [Fastify](fastify.md) · [NestJS](nestjs.md) · [Next.js](nextjs.md) · [LoopBack](loopback.md) · [GraphQL](graphql-typescript.md) · [AWS Lambda](aws-lambda-typescript.md) |
| TypeScript / JavaScript — frontends | [Angular](angular.md) · [React Router](react.md) · [Vue](vue.md) |
| GraphQL schema files | [SDL](graphql-sdl.md) |
| SOAP contract files | [WSDL](wsdl.md) |
| Java / Groovy / Kotlin | [Spring](spring.md) · [JAX-RS](jaxrs.md) · [Vert.x](vertx.md) · [Ktor](ktor.md) · [Quartz](quartz.md) |
| Scala | [Play](play.md) · [Akka HTTP](akka-http.md) · [http4s](http4s.md) · [Akka actors](akka.md) |
| C# / VB.NET | [ASP.NET](aspnet.md) · [Web Forms](webforms.md) · [WCF / ASMX](wcf.md) · [HotChocolate](hotchocolate.md) · [graphql-dotnet](graphql-dotnet.md) · [AWS Lambda](lambda-csharp.md) · [Quartz.NET](quartz.md) |
| Python | [FastAPI](fastapi.md) · [Celery](celery.md) |
| Ruby | [Rails](rails.md) · [Sinatra](sinatra.md) · [Grape](grape.md) |
| PHP | [Laravel](laravel.md) · [CodeIgniter](codeigniter.md) · [Slim](slim.md) · [Symfony](symfony.md) · [WordPress](wordpress.md) |
| Go | [net/http](net-http.md) · [Gin](gin.md) · [Echo](echo.md) · [chi](chi.md) · [Fiber](fiber.md) · [gorilla/mux](gorilla-mux.md) |
