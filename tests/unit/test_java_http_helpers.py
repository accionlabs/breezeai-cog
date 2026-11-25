"""Java HTTP calls made through the application's own helpers are recorded where they run
(BREEZEAI-1654, ``parsers/java_http_helpers.py``; contract §5 "Where an outbound record is
written")."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog import iter_file_records

P = "src/com/acme/"
V2 = "import org.vertx.java.core.http.HttpClient;\nimport org.vertx.java.core.http.HttpClientRequest;\n"
UTIL = ("package com.acme;\npublic class ApiUtil {\n"
        "  public static final String METHOD_GET = \"GET\";\n"
        "  public static final String METHOD_POST = \"POST\";\n}\n")
API = f"""package com.acme;
{V2}public class ApiHandler {{
  protected HttpClient client;
  protected String apiPath;
  public HttpClientRequest getClient(String url, Object headers) {{
    return requestClient(ApiUtil.METHOD_GET, url, null, headers);
  }}
  public HttpClientRequest postClient(String url, Object body, Object headers) {{
    return requestClient(ApiUtil.METHOD_POST, url, body, headers);
  }}
  public HttpClientRequest requestClient(String method, String url, Object body, Object h) {{
    HttpClientRequest request = client.request(method, apiPath + url, null);
    return request;
  }}
  public void health() {{
    HttpClientRequest r = client.get("/health", null);
  }}
}}
"""


@pytest.fixture
def api_calls(tmp_path: Path):
    """``{path: source}`` → sorted (file, line, method, endpoint) of every ``api_call``."""

    def run(files: dict[str, str]) -> list[tuple[str, int, str | None, str | None]]:
        repo = tmp_path / "repo"
        for rel, src in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src)
        return sorted((r.path.rsplit("/", 1)[-1], s.startLine, s.method, s.endpoint)
                      for r in iter_file_records(repo) for s in r.statements
                      if s.semanticType == "api_call")

    return run


def test_call_through_relay_and_helper_is_the_request(api_calls) -> None:
    got = api_calls({P + "ApiUtil.java": UTIL, P + "ApiHandler.java": API,
                     P + "FileApiHandler.java": f"""package com.acme;
{V2}public class FileApiHandler extends ApiHandler {{
  public void fileId() {{
    String url = "id";
    HttpClientRequest r = getClient(url, null);
  }}
  public void upload(String fileId) {{
    HttpClientRequest r = postClient("/" + fileId + "/upload", "b", null);
  }}
  public void raw(String method, String u) {{
    HttpClientRequest r = requestClient(method, u, null, null);
  }}
}}
"""})
    assert got == [
        # an ordinary request keeps its record; the helper's and the relays' do not
        ("ApiHandler.java", 18, "GET", "/health"),
        # inherited relay, verb from the constant it passes; a local with one constant value
        ("FileApiHandler.java", 7, "GET", "${apiPath}id"),
        ("FileApiHandler.java", 10, "POST", "${apiPath}/{fileId}/upload"),
        # `raw` passes its own parameters on: a relay itself, no record
    ]


def test_helper_with_no_caller_has_no_record(api_calls) -> None:
    got = api_calls({P + "ApiUtil.java": UTIL, P + "ApiHandler.java": API})
    assert got == [("ApiHandler.java", 18, "GET", "/health")]


def test_typed_receiver_and_verb_argument(api_calls) -> None:
    got = api_calls({P + "ApiUtil.java": UTIL, P + "ApiHandler.java": API,
                     P + "Caller.java": f"""package com.acme;
{V2}public class Caller {{
  private ApiHandler api;
  public void run() {{
    HttpClientRequest r = api.requestClient("put", "/orders/1", null, null);
    HttpClientRequest s = api.getClient(ApiUtil.METHOD_GET, null);
  }}
}}
"""})
    assert ("Caller.java", 7, "PUT", "${apiPath}/orders/1") in got
    # an argument that is a constant folds like any other
    assert ("Caller.java", 8, "GET", "${apiPath}GET") in got


def test_unproven_helper_keeps_its_record(api_calls) -> None:
    # the URL parameter is rewritten in the method, and another uses two parameters in it
    got = api_calls({P + "Odd.java": f"""package com.acme;
{V2}public class Odd {{
  private HttpClient client;
  public void rewritten(String url) {{
    url = url.trim();
    HttpClientRequest r = client.get(url, null);
  }}
  public void two(String host, String path) {{
    HttpClientRequest r = client.get(host + path, null);
  }}
  public void call() {{ rewritten("/a"); two("h", "/b"); }}
}}
"""})
    assert [(f, line) for f, line, _, _ in got] == [("Odd.java", 8), ("Odd.java", 11)]


def test_apache_helper_and_static_call(api_calls) -> None:
    got = api_calls({
        P + "Http.java": """package com.acme;
import org.apache.http.client.methods.HttpGet;
public class Http {
  public static HttpGet get(String uri) { return new HttpGet("https://api.acme.com" + uri); }
}
""",
        P + "Caller.java": """package com.acme;
public class Caller {
  public void run() { Http.get("/v1/orders"); }
}
""",
    })
    assert got == [("Caller.java", 3, "GET", "https://api.acme.com/v1/orders")]


def test_decorator_of_the_client_is_a_helper(api_calls) -> None:
    # CatchableHttpClient: wraps every request method of the client it is given
    got = api_calls({P + "Catchable.java": f"""package com.acme;
{V2}import org.vertx.java.core.Handler;
public class Catchable {{
  private HttpClient client;
  public HttpClientRequest get(String uri, Handler h) {{ return client.get(uri, h); }}
  public HttpClientRequest post(String uri, Handler h) {{ return client.post(uri, h); }}
}}
""", P + "User.java": f"""package com.acme;
{V2}public class User {{
  private Catchable http;
  public void run() {{ HttpClientRequest r = http.post("/orders", null); }}
}}
"""})
    assert got == [("User.java", 6, "POST", "/orders")]


def test_varargs_helper_and_relay(api_calls) -> None:
    # P3's shape: `ResponseHandler... endHandler` after the URL — the fixed positions decide
    got = api_calls({P + "ApiUtil.java": UTIL, P + "Api.java": f"""package com.acme;
{V2}public class Api {{
  private HttpClient client;
  public HttpClientRequest getClient(String url, Object h, Object... end) {{
    return requestClient(ApiUtil.METHOD_GET, url, null, h, end);
  }}
  public HttpClientRequest requestClient(String m, String url, Object b, Object h, Object... end) {{
    return client.request(m, url, null);
  }}
}}
""", P + "Use.java": f"""package com.acme;
{V2}public class Use extends Api {{
  public void a() {{ getClient("/a", null); }}
  public void b() {{ getClient("/b", null, "x", "y"); }}
}}
"""})
    assert got == [("Use.java", 5, "GET", "/a"), ("Use.java", 6, "GET", "/b")]


def test_method_naming_its_own_endpoint_is_no_helper(api_calls) -> None:
    got = api_calls({P + "Orders.java": f"""package com.acme;
{V2}public class Orders {{
  private HttpClient client;
  public void load(String base) {{ HttpClientRequest r = client.get(base + "/orders", null); }}
  public void call() {{ load("http://svc"); }}
}}
"""})
    assert got == [("Orders.java", 6, "GET", "${base}/orders")]
