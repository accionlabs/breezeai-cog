"""AWS Lambda (Go) role table (docs/entry-point-roles/lambda-go.md): one test per row."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

MOD = "module example.com/app\n\ngo 1.22\n"
HEAD = ('package main\n\nimport (\n\t"context"\n\n\t"github.com/aws/aws-lambda-go/events"\n'
        '\t"github.com/aws/aws-lambda-go/lambda"\n)\n\n')


def _eps(analyze_files, body: str):
    st = analyze_files({"go.mod": MOD, "main.go": HEAD + body})
    return [(s.semanticType, s.routeKind, s.framework, s.method, s.endpoint, s.handler, s.handlerLine)
            for s in st if is_entry_point(s)]


def test_event_source_handler(analyze_files) -> None:
    assert _eps(analyze_files, "func handler(ctx context.Context, e events.SQSEvent) error { return nil }\n\n"
                               "func main() {\n\tlambda.Start(handler)\n}\n") \
        == [("eventbus_consumer", None, "aws-sqs", None, None, "handler", 10)]


def test_http_handler(analyze_files) -> None:
    assert _eps(analyze_files, "func api(r events.APIGatewayProxyRequest) (events.APIGatewayProxyResponse, error) {\n"
                               "\treturn events.APIGatewayProxyResponse{}, nil\n}\n\n"
                               "func main() {\n\tlambda.Start(api)\n}\n") \
        == [("route", "route", "aws-apigw", None, None, "api", 10)]


def test_other_event_is_a_lambda_entry(analyze_files) -> None:
    assert _eps(analyze_files, "func auth(r events.APIGatewayCustomAuthorizerRequest) error { return nil }\n"
                               "func plain() {}\n\nfunc main() {\n\tlambda.Start(auth)\n\tlambda.StartHandlerFunc(plain)\n}\n") \
        == [("eventbus_consumer", None, "aws-lambda", None, None, "auth", 10),
            ("eventbus_consumer", None, "aws-lambda", None, None, "plain", 11)]


def test_function_literal(analyze_files) -> None:
    assert _eps(analyze_files, "func main() {\n\tlambda.Start(\n\t\tfunc(ctx context.Context, e *events.SNSEvent) error { return nil })\n}\n") \
        == [("eventbus_consumer", None, "aws-sns", None, None, None, 12)]


def test_wrapper_of_a_function_of_the_file(analyze_files) -> None:
    assert _eps(analyze_files, "func handler(e events.S3Event) error { return nil }\n\n"
                               "func main() {\n\tlambda.Start(otel.WrapHandler(handler))\n}\n") \
        == [("eventbus_consumer", None, "aws-s3", None, None, "handler", 10)]


def test_handler_built_at_run_time(analyze_files) -> None:
    assert _eps(analyze_files, "func main() {\n\tlambda.Start(newHandler(cfg))\n}\n") \
        == [("eventbus_consumer", None, "aws-lambda", None, None, None, None)]
