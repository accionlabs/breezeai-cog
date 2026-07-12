"""WCF parser (C#): [ServiceContract]/[OperationContract] → route detection (off the
record), REST-over-WCF ([WebGet]/[WebInvoke]), capture-gating, and parser selection."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.csharp.parser import CSharpParser
from breezeai_cog.parsers.csharp_aspnet.parser import AspNetCoreParser
from breezeai_cog.parsers.csharp_webforms.parser import WebFormsParser
from breezeai_cog.parsers.csharp_wcf.parser import WcfParser
from breezeai_cog.schemas import FileRecord

SOAP = b'''
using System.ServiceModel;
namespace KUCare {
  [ServiceContract(Name = "EnrollmentService")]
  public interface IEnrollmentService {
    [OperationContract]
    ResponseId SaveEnrollment(EnrollmentDto dto);
    [OperationContract]
    Task<Order> GetOrder(long id);
    int NotAnOperation(int x);   // no [OperationContract] -> not a route
  }
}
'''

REST = b'''
using System.ServiceModel;
using System.ServiceModel.Web;
namespace KUCare {
  [ServiceContract]
  public interface ICRCMService {
    [OperationContract]
    [WebGet(UriTemplate = "/crcm?input={jsonInput}")]
    string CRCMDataPortal(string jsonInput);
    [OperationContract]
    [WebInvoke(Method = "PUT", UriTemplate = "/crcm/save")]
    void Save(CrcmDto dto);
  }
}
'''

MVC = b'''
using Microsoft.AspNetCore.Mvc;
namespace Acme {
  [ApiController] [Route("api/orders")]
  public class OrdersController : ControllerBase {
    [HttpGet] public object Get() { return null; }
  }
}
'''

WEBFORM = b'''
using System;
using System.Web.UI;
namespace Acme { public partial class Enrollment : Page {
  protected void Page_Load(object s, EventArgs e) { }
} }
'''

COREWCF = b'''
using CoreWCF;
namespace K { [ServiceContract] public interface IPingService {
  [OperationContract] string Ping();
} }
'''

FULLFORM = b'''
using System.ServiceModel;
namespace K { [ServiceContractAttribute] public interface IFullService {
  [OperationContractAttribute] string DoWork();
} }
'''

ASMX = b'''
using System.Web.Services;
namespace K {
  [WebService(Namespace = "http://k/")]
  public class LegacyService : System.Web.Services.WebService {
    [WebMethod] public Task<Report> GetData(int id) { return null; }
    public string NotExposed() { return null; }
  }
}
'''


def _parse(parser, src, name, *, capture=True) -> FileRecord:
    ctx = ParseContext(path=name, abs_path=None, source=src, repo_root=None, capture_statements=capture)
    return parser.parse_file(ctx)


def _routes(rec):
    return {s.handler: s for s in rec.statements if s.semanticType == "route"}


def test_routes_require_capture() -> None:
    rec = _parse(WcfParser(), SOAP, "IEnrollmentService.cs", capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_soap_operations() -> None:
    rec = _parse(WcfParser(), SOAP, "IEnrollmentService.cs")
    routes = _routes(rec)
    assert set(routes) == {"SaveEnrollment", "GetOrder"}   # NotAnOperation excluded
    save = routes["SaveEnrollment"]
    assert save.framework == "wcf"
    assert save.nodeType == "synthetic"       # normalized synthetic marker (was "attribute")
    assert save.method == "RPC"
    assert save.routeKind == "rpc"
    assert save.endpoint == "EnrollmentService/SaveEnrollment"   # [ServiceContract(Name=…)]
    assert routes["GetOrder"].responseDTO == "Order"             # Task<Order> unwrapped
    assert rec.framework == "wcf"


def test_rest_over_wcf() -> None:
    rec = _parse(WcfParser(), REST, "ICRCMService.cs")
    routes = _routes(rec)
    get = routes["CRCMDataPortal"]
    assert get.method == "GET"                       # [WebGet]
    assert get.routeKind == "route"
    assert get.endpoint == "/crcm?input={jsonInput}"  # UriTemplate wins
    put = routes["Save"]
    assert put.method == "PUT"                       # [WebInvoke(Method="PUT")]
    assert put.endpoint == "/crcm/save"


def test_default_service_name_strips_leading_i() -> None:
    # no Name= arg → ISiteService → SiteService
    src = b"using System.ServiceModel;\nnamespace X {\n[ServiceContract] public interface ISiteService {\n[OperationContract] void Ping();\n} }"
    rec = _parse(WcfParser(), src, "ISiteService.cs")
    assert _routes(rec)["Ping"].endpoint == "SiteService/Ping"


def test_service_name_override_differs_from_interface() -> None:
    # [ServiceContract(Name="AgencyService")] on ISPSContractService → AgencyService (not SPSContractService)
    src = (b'using System.ServiceModel;\nnamespace X {\n'
           b'[ServiceContract(Name = "AgencyService", Namespace = "http://kucare/sps")]\n'
           b'public interface ISPSContractService {\n[OperationContract] string HelloWorld();\n} }')
    rec = _parse(WcfParser(), src, "ISPSContractService.cs")
    assert _routes(rec)["HelloWorld"].endpoint == "AgencyService/HelloWorld"


def test_generated_client_proxy_skipped() -> None:
    # svcutil-generated client proxy: [GeneratedCode] + full-form [ServiceContractAttribute].
    # It's the client side, not a server entry point — must NOT produce routes.
    src = (b"using System.ServiceModel;\nnamespace K {\n"
           b"[System.CodeDom.Compiler.GeneratedCodeAttribute(\"svcutil\", \"6.0\")]\n"
           b"[ServiceContractAttribute]\n"
           b"public interface ICatalogServiceProxy {\n"
           b"[OperationContractAttribute] string FindCatalogItem(int id);\n} }")
    rec = _parse(WcfParser(), src, "Reference.cs")
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_non_service_file_has_no_routes() -> None:
    # Uses System.ServiceModel (a client proxy) but defines no [ServiceContract] → no route.
    src = b"using System.ServiceModel;\nnamespace X { public class Proxy { public void Call(){} } }"
    rec = _parse(WcfParser(), src, "Proxy.cs")
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_corewcf_is_detected() -> None:
    rec = _parse(WcfParser(), COREWCF, "IPingService.cs")   # claims on `using CoreWCF;`
    assert _routes(rec)["Ping"].framework == "wcf"


def test_full_form_attribute_names() -> None:
    # [ServiceContractAttribute] / [OperationContractAttribute] (full form) must still match
    rec = _parse(WcfParser(), FULLFORM, "IFullService.cs")
    assert set(_routes(rec)) == {"DoWork"}
    assert _routes(rec)["DoWork"].endpoint == "FullService/DoWork"


def test_asmx_webmethod() -> None:
    rec = _parse(WcfParser(), ASMX, "Legacy.asmx.cs")
    routes = _routes(rec)
    assert set(routes) == {"GetData"}                 # NotExposed (no [WebMethod]) excluded
    r = routes["GetData"]
    assert r.framework == "asmx"
    assert r.method == "RPC" and r.routeKind == "rpc"
    assert r.endpoint == "LegacyService/GetData"      # [WebService] Name absent → class name
    assert r.responseDTO == "Report"                  # Task<Report> unwrapped
    assert rec.framework == "asmx"


def test_output_validates() -> None:
    for src, name in [(SOAP, "IEnrollmentService.cs"), (REST, "ICRCMService.cs"),
                      (ASMX, "Legacy.asmx.cs"), (COREWCF, "IPingService.cs")]:
        rec = _parse(WcfParser(), src, name)
        errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                      .iter_errors(json.loads(to_line(rec))))
        assert not errors, errors


def test_selection() -> None:
    registry.clear()
    for p in (CSharpParser(), AspNetCoreParser(), WebFormsParser(), WcfParser()):
        registry.register(p)
    assert registry.select("IEnrollmentService.cs", SOAP).name == "csharp-wcf"
    assert registry.select("Legacy.asmx.cs", ASMX).name == "csharp-wcf"       # ASMX
    assert registry.select("IPingService.cs", COREWCF).name == "csharp-wcf"   # CoreWCF
    assert registry.select("Orders.cs", MVC).name == "csharp-aspnet"
    # a WCF-consuming page stays with webforms (wcf excludes .aspx.cs)
    assert registry.select("CMS/Enrollment.aspx.cs", WEBFORM + b"\nusing System.ServiceModel;").name == "csharp-webforms"
    assert registry.select("plain.cs", b"class C {}").name == "csharp"
    registry.clear()
