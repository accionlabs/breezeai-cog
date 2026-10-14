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

SOAP = b"""
using System.ServiceModel;
namespace Acme {
  [ServiceContract(Name = "OrderService")]
  public interface IOrderService {
    [OperationContract]
    ResponseId SaveOrder(OrderDto dto);
    [OperationContract]
    Task<Order> GetOrder(long id);
    int NotAnOperation(int x);   // no [OperationContract] -> not a route
  }
}
"""

REST = b"""
using System.ServiceModel;
using System.ServiceModel.Web;
namespace Acme {
  [ServiceContract]
  public interface IPricingService {
    [OperationContract]
    [WebGet(UriTemplate = "/pricing?input={jsonInput}")]
    string GetPrice(string jsonInput);
    [OperationContract]
    [WebInvoke(Method = "PUT", UriTemplate = "/pricing/save")]
    void Save(PriceDto dto);
  }
}
"""

MVC = b"""
using Microsoft.AspNetCore.Mvc;
namespace Acme {
  [ApiController] [Route("api/orders")]
  public class OrdersController : ControllerBase {
    [HttpGet] public object Get() { return null; }
  }
}
"""

WEBFORM = b"""
using System;
using System.Web.UI;
namespace Acme { public partial class Checkout : Page {
  protected void Page_Load(object s, EventArgs e) { }
} }
"""

COREWCF = b"""
using CoreWCF;
namespace K { [ServiceContract] public interface IPingService {
  [OperationContract] string Ping();
} }
"""

FULLFORM = b"""
using System.ServiceModel;
namespace K { [ServiceContractAttribute] public interface IFullService {
  [OperationContractAttribute] string DoWork();
} }
"""

ASMX = b"""
using System.Web.Services;
namespace K {
  [WebService(Namespace = "http://k/")]
  public class LegacyService : System.Web.Services.WebService {
    [WebMethod] public Task<Report> GetData(int id) { return null; }
    public string NotExposed() { return null; }
  }
}
"""


def _parse(parser, src, name, *, capture=True) -> FileRecord:
    ctx = ParseContext(
        path=name, abs_path=None, source=src, repo_root=None, capture_statements=capture
    )
    return parser.parse_file(ctx)


def _routes(rec):
    return {s.handler: s for s in rec.statements if s.semanticType == "route"}


def test_routes_require_capture() -> None:
    rec = _parse(WcfParser(), SOAP, "IOrderService.cs", capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_soap_operations() -> None:
    rec = _parse(WcfParser(), SOAP, "IOrderService.cs")
    routes = _routes(rec)
    assert set(routes) == {"SaveOrder", "GetOrder"}  # NotAnOperation excluded
    save = routes["SaveOrder"]
    assert save.framework == "wcf"
    assert save.nodeType == "synthetic"  # normalized synthetic marker (was "attribute")
    assert save.method == "RPC"
    assert save.routeKind == "rpc"
    assert save.endpoint == "OrderService/SaveOrder"  # [ServiceContract(Name=…)]
    assert routes["GetOrder"].responseDTO == "Order"  # Task<Order> unwrapped
    assert rec.framework == "wcf"


def test_rest_over_wcf() -> None:
    rec = _parse(WcfParser(), REST, "IPricingService.cs")
    routes = _routes(rec)
    get = routes["GetPrice"]
    assert get.method == "GET"  # [WebGet]
    assert get.routeKind == "route"
    # no .svc for this contract → the address is unknown: empty, never the bare UriTemplate
    assert get.endpoint is None
    put = routes["Save"]
    assert put.method == "PUT"  # [WebInvoke(Method="PUT")]
    assert put.endpoint is None


_SVC_CB = """using System.ServiceModel;
using System.ServiceModel.Web;

namespace Shop.Import
{
    [ServiceContract]
    public class Rest
    {
        [OperationContract]
        [WebInvoke(Method = "POST", BodyStyle = WebMessageBodyStyle.WrappedRequest)]
        public string GetImportItem(string id) { return ""; }

        [OperationContract]
        [WebGet(UriTemplate = "/items/{id}?fmt={fmt}")]
        public string FindItem(string id, string fmt) { return ""; }

        [OperationContract]
        public string Ping() { return ""; }
    }
}
"""


def test_rest_endpoint_is_under_the_svc(analyze_files) -> None:
    stmts = analyze_files({
        "Import/Rest.svc": '<%@ ServiceHost Service="Shop.Import.Rest" CodeBehind="Rest.svc.cs" %>',
        "Import/Rest.svc.cs": _SVC_CB,
        # same class shape, but its .svc names another class → host unknown
        "Other/Rest.svc": '<%@ ServiceHost Service="Shop.Other.Service" %>',
        "Other/Rest.svc.cs": _SVC_CB.replace("Shop.Import", "Shop.Other"),
    })
    got = {(s.path, s.handler): (s.method, s.endpoint)
           for s in stmts if s.framework == "wcf" and s.semanticType == "route" and s.handler}
    assert got[("Import/Rest.svc.cs", "GetImportItem")] == ("POST", "/Import/Rest.svc/GetImportItem")
    assert got[("Import/Rest.svc.cs", "FindItem")] == ("GET", "/Import/Rest.svc/items/{id}")
    assert got[("Import/Rest.svc.cs", "Ping")] == ("RPC", "Rest/Ping")  # SOAP: <Contract>/<Operation>
    assert got[("Other/Rest.svc.cs", "GetImportItem")] == ("POST", None)


def test_default_service_name_strips_leading_i() -> None:
    # no Name= arg → ISiteService → SiteService
    src = b"using System.ServiceModel;\nnamespace X {\n[ServiceContract] public interface ISiteService {\n[OperationContract] void Ping();\n} }"
    rec = _parse(WcfParser(), src, "ISiteService.cs")
    assert _routes(rec)["Ping"].endpoint == "SiteService/Ping"


def test_service_name_override_differs_from_interface() -> None:
    # [ServiceContract(Name="AgencyService")] on IPartnerService → AgencyService (not PartnerService)
    src = (
        b"using System.ServiceModel;\nnamespace X {\n"
        b'[ServiceContract(Name = "AgencyService", Namespace = "http://acme/partner")]\n'
        b"public interface IPartnerService {\n[OperationContract] string HelloWorld();\n} }"
    )
    rec = _parse(WcfParser(), src, "IPartnerService.cs")
    assert _routes(rec)["HelloWorld"].endpoint == "AgencyService/HelloWorld"


def test_generated_client_proxy_skipped() -> None:
    # svcutil-generated client proxy: [GeneratedCode] + full-form [ServiceContractAttribute].
    # It's the client side, not a server entry point — must NOT produce routes.
    src = (
        b"using System.ServiceModel;\nnamespace K {\n"
        b'[System.CodeDom.Compiler.GeneratedCodeAttribute("svcutil", "6.0")]\n'
        b"[ServiceContractAttribute]\n"
        b"public interface ICatalogServiceProxy {\n"
        b"[OperationContractAttribute] string FindCatalogItem(int id);\n} }"
    )
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
    rec = _parse(WcfParser(), COREWCF, "IPingService.cs")  # claims on `using CoreWCF;`
    assert _routes(rec)["Ping"].framework == "wcf"


def test_full_form_attribute_names() -> None:
    # [ServiceContractAttribute] / [OperationContractAttribute] (full form) must still match
    rec = _parse(WcfParser(), FULLFORM, "IFullService.cs")
    assert set(_routes(rec)) == {"DoWork"}
    assert _routes(rec)["DoWork"].endpoint == "FullService/DoWork"


def test_asmx_webmethod() -> None:
    rec = _parse(WcfParser(), ASMX, "Legacy.asmx.cs")
    routes = _routes(rec)
    assert set(routes) == {"GetData"}  # NotExposed (no [WebMethod]) excluded
    r = routes["GetData"]
    assert r.framework == "asmx"
    assert r.method == "RPC" and r.routeKind == "rpc"
    assert r.endpoint is None  # no .asmx host file in this test: the address is not known
    assert r.responseDTO == "Report"  # Task<Report> unwrapped
    assert rec.framework == "asmx"


def test_output_validates() -> None:
    for src, name in [
        (SOAP, "IOrderService.cs"),
        (REST, "IPricingService.cs"),
        (ASMX, "Legacy.asmx.cs"),
        (COREWCF, "IPingService.cs"),
    ]:
        rec = _parse(WcfParser(), src, name)
        errors = list(
            Draft202012Validator(FileRecord.model_json_schema(by_alias=True)).iter_errors(
                json.loads(to_line(rec))
            )
        )
        assert not errors, errors


def test_selection() -> None:
    registry.clear()
    for p in (CSharpParser(), AspNetCoreParser(), WebFormsParser(), WcfParser()):
        registry.register(p)
    assert registry.select("IOrderService.cs", SOAP).name == "csharp-wcf"
    assert registry.select("Legacy.asmx.cs", ASMX).name == "csharp-wcf"  # ASMX
    assert registry.select("IPingService.cs", COREWCF).name == "csharp-wcf"  # CoreWCF
    assert registry.select("Orders.cs", MVC).name == "csharp-aspnet"
    # a WCF-consuming page stays with webforms (wcf excludes .aspx.cs)
    assert (
        registry.select("CMS/Checkout.aspx.cs", WEBFORM + b"\nusing System.ServiceModel;").name
        == "csharp-webforms"
    )
    assert registry.select("plain.cs", b"class C {}").name == "csharp"
    registry.clear()


def test_asmx_endpoint_is_the_asmx_file_and_method(analyze_files) -> None:
    # <.asmx path>/<Method> when X.asmx sits beside X.asmx.cs and its Class= names the class;
    # otherwise empty (a service or class name is not an address).
    stmts = analyze_files({
        "Svc/Legacy.asmx": '<%@ WebService Language="C#" CodeBehind="Legacy.asmx.cs" Class="K.LegacyService" %>',
        "Svc/Legacy.asmx.cs": ASMX.decode(),
        "Other/Wrong.asmx": '<%@ WebService Class="K.SomethingElse" %>',
        "Other/Wrong.asmx.cs": ASMX.decode(),
        "App_Code/Loose.cs": ASMX.decode(),
    })
    got = {s.path: s.endpoint for s in stmts if s.framework == "asmx" and s.path.endswith(".cs")}
    assert got == {
        "Svc/Legacy.asmx.cs": "Svc/Legacy.asmx/GetData",
        "Other/Wrong.asmx.cs": None,   # the .asmx serves another class
        "App_Code/Loose.cs": None,     # no .asmx beside it
    }


_CONTRACT = """using System.ServiceModel;
namespace Shop {
[ServiceContract]
public interface IOrderService {
    [OperationContract] Order GetOrder(int id);
    [OperationContract] void Cancel(int id);
}
}
"""


def _wcf_handlers(stmts) -> dict[str, tuple[str | None, int | None]]:
    return {s.endpoint: (s.handler, s.handlerLine)
            for s in stmts if s.framework == "wcf" and s.semanticType == "route"}


def test_interface_contract_handler_is_the_implementing_method(analyze_files) -> None:
    # One implementer in another file: the method with a body runs → Class.Method, no line
    # (the record is in the interface's file; handlerPath does not exist yet).
    stmts = analyze_files({
        "IOrderService.cs": _CONTRACT,
        "OrderService.svc.cs": (
            "namespace Shop {\npublic class OrderService : ServiceBase, IOrderService {\n"
            "    public Order GetOrder(int id) { return null; }\n"
            "    void IOrderService.Cancel(int id) { }\n}\n}\n"  # explicit implementation
        ),
    })
    assert _wcf_handlers(stmts) == {
        "OrderService/GetOrder": ("OrderService.GetOrder", None),
        "OrderService/Cancel": ("OrderService.Cancel", None),
    }


def test_interface_contract_with_two_implementers_keeps_the_interface_method(analyze_files) -> None:
    stmts = analyze_files({
        "IOrderService.cs": _CONTRACT,
        "A.cs": "namespace Shop { public class A : IOrderService { public Order GetOrder(int id) => null; public void Cancel(int id) {} } }\n",
        "B.cs": "namespace Shop { public class B : IOrderService { public Order GetOrder(int id) => null; public void Cancel(int id) {} } }\n",
    })
    assert _wcf_handlers(stmts)["OrderService/GetOrder"] == ("GetOrder", 5)


def test_implementer_in_the_same_file_gets_its_line(analyze_files) -> None:
    src = _CONTRACT.replace("}\n}\n", "}\npublic class OrderService : IOrderService {\n"
                            "    public Order GetOrder(int id) => null;\n"
                            "    public void Cancel(int id) { }\n}\n}\n", 1)
    stmts = analyze_files({"OrderService.cs": src})
    assert _wcf_handlers(stmts)["OrderService/GetOrder"] == ("OrderService.GetOrder", 9)


def test_concrete_service_contract_is_unchanged(analyze_files) -> None:
    stmts = analyze_files({
        "Svc.cs": "using System.ServiceModel;\nnamespace Shop {\n[ServiceContract]\npublic class Svc {\n"
                  "    [OperationContract] public void Ping() { }\n}\n}\n",
    })
    assert _wcf_handlers(stmts) == {"Svc/Ping": ("Ping", 5)}
