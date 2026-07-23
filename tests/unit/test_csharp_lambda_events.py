"""AWS Lambda handler detection (C#): an ADDITIVE detector on the base CSharpParser —
FunctionHandler entry points gated on an AWS event parameter + ILambdaContext, helper-method
rejection, HTTP-event route kind, capture-gating, and co-existence with an ASP.NET controller
in the same file (the reason it's additive, not a claiming parser)."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.csharp.parser import CSharpParser
from breezeai_cog.parsers.csharp_aspnet.parser import AspNetCoreParser
from breezeai_cog.schemas import FileRecord

# An event-driven Lambda: FunctionHandler is the entry point; ImportData/CleanUp are helpers
# that merely PASS ILambdaContext through (no event type) — they must NOT be tagged.
LAMBDA = b"""
using Amazon.Lambda.Core;
using Amazon.Lambda.S3Events;

[assembly: LambdaSerializer(typeof(Amazon.Lambda.Serialization.Json.JsonSerializer))]

namespace Nimbus {
  public class Function {
    public async Task FunctionHandler(S3Event s3Event, ILambdaContext context) {
      await ImportData(context, "/tmp/x");
    }
    private async Task ImportData(ILambdaContext context, string tempPath) { }
    private void CleanUpTempFiles(ILambdaContext context) { }
  }
}
"""

# HTTP-facing Lambda (APIGateway) → routeKind "route", not "event".
APIGW = b"""
using Amazon.Lambda.Core;
using Amazon.Lambda.APIGatewayEvents;
namespace Nimbus {
  public class ApiFunction {
    public APIGatewayProxyResponse Handle(APIGatewayProxyRequest request, ILambdaContext context) {
      return new APIGatewayProxyResponse { StatusCode = 200 };
    }
  }
}
"""


def _parse(tmp_path, rel: str, src: bytes, parser=None, capture=True) -> FileRecord:
    p = tmp_path / rel
    p.write_bytes(src)
    ctx = ParseContext(path=rel, abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=capture)
    return (parser or CSharpParser()).parse_file(ctx)


def _routes(rec: FileRecord) -> dict[str, object]:
    return {s.handler: s for s in rec.statements if s.framework == "aws-lambda"}


def test_event_handler_detected(tmp_path) -> None:
    rec = _parse(tmp_path, "Function.cs", LAMBDA)
    handlers = _routes(rec)
    # only the FunctionHandler (event type + ILambdaContext) is an entry point.
    assert set(handlers) == {"FunctionHandler"}
    fh = handlers["FunctionHandler"]
    assert fh.framework == "aws-lambda"
    # S3-event trigger → eventbus_consumer (aligned with TS aws_events), not a route.
    assert fh.semanticType == "eventbus_consumer"
    assert fh.routeKind is None
    assert fh.endpoint == "FunctionHandler"
    assert fh.handlerLine == fh.startLine
    assert fh.parentId == rec.id
    assert rec.framework == "aws-lambda"


def test_helper_methods_not_tagged(tmp_path) -> None:
    # ImportData / CleanUpTempFiles take ILambdaContext but NO event type → not entry points.
    rec = _parse(tmp_path, "Function.cs", LAMBDA)
    fn_names = {f.name for f in rec.functions}
    assert {"ImportData", "CleanUpTempFiles"} <= fn_names  # captured as functions
    tagged = {s.handler for s in rec.statements if s.framework == "aws-lambda"}
    assert "ImportData" not in tagged and "CleanUpTempFiles" not in tagged


def test_apigateway_handler_is_route(tmp_path) -> None:
    rec = _parse(tmp_path, "ApiFunction.cs", APIGW)
    handlers = _routes(rec)
    assert set(handlers) == {"Handle"}
    # HTTP-facing event → route (matches TS aws_events APIGateway handling), not a consumer.
    assert handlers["Handle"].semanticType == "route"
    assert handlers["Handle"].routeKind == "route"
    assert handlers["Handle"].framework == "aws-lambda"


def test_requires_capture_statements(tmp_path) -> None:
    rec = _parse(tmp_path, "Function.cs", LAMBDA, capture=False)
    assert [s for s in rec.statements if s.framework == "aws-lambda"] == []


def test_fixture_files_skipped(tmp_path) -> None:
    # Entry-point emitters skip fixture files (per the reliability checklist) — a handler in a
    # .test./.spec. file must not be captured as a live entry point.
    rec = _parse(tmp_path, "Function.test.cs", LAMBDA)
    assert [s for s in rec.statements if s.framework == "aws-lambda"] == []
    # base extraction still runs — the class/method are captured, just not the entry point.
    assert "Function" in {c.name for c in rec.classes}


def test_base_extraction_reused(tmp_path) -> None:
    rec = _parse(tmp_path, "Function.cs", LAMBDA)
    assert rec.language == "csharp"
    assert "Function" in {c.name for c in rec.classes}


def test_coexists_with_aspnet_controller(tmp_path) -> None:
    # The reason this is additive, not a claiming parser: an ASP.NET app hosted on Lambda
    # (Amazon.Lambda.AspNetCoreServer) has BOTH a controller route AND a Lambda handler in
    # one file. Both must be captured — a one-parser-per-file model would drop one.
    src = b'''using Microsoft.AspNetCore.Mvc;
using Amazon.Lambda.Core;
[ApiController]
[Route("api/orders")]
public class OrdersController : ControllerBase {
  [HttpGet] public IActionResult List() => Ok();
  public async Task Handler(SQSEvent evt, ILambdaContext ctx) { }
}'''
    # AspNetCoreParser owns the file (its claims win); the additive Lambda detector still runs
    # via the inherited CSharpParser.extract.
    rec = _parse(tmp_path, "OrdersController.cs", src, parser=AspNetCoreParser())
    tagged = {(s.semanticType, s.framework) for s in rec.statements if s.semanticType}
    assert ("route", "aspnet") in tagged                 # controller route preserved
    assert ("eventbus_consumer", "aws-lambda") in tagged  # SQS Lambda handler ALSO captured


def test_no_registry_parser_for_lambda() -> None:
    # Lambda detection is additive (csharp/lambda_events.py) — NOT a claiming parser, so it
    # must not be registered (no selection surface).
    from breezeai_cog.core import registry
    registry.discover_builtin()
    assert "csharp-lambda" not in {p.name for p in registry._REGISTRY}


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, "Function.cs", LAMBDA)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
