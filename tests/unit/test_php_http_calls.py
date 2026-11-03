"""PHP outbound HTTP calls are recorded only for a proven client (php/http_calls.py)."""

from __future__ import annotations

HEAD = (
    "<?php\nnamespace App;\nuse Illuminate\\Support\\Facades\\Http;\nuse GuzzleHttp\\Client;\n"
    "use Symfony\\Contracts\\HttpClient\\HttpClientInterface;\nuse Symfony\\Component\\HttpClient\\HttpClient;\n"
    "use Psr\\Http\\Client\\ClientInterface;\n"
)


def _api(analyze_files, body: str):
    stmts = analyze_files({"app/S.php": HEAD + "class S {\n" + body + "}\n"})
    return sorted((s.text.strip().rstrip(";"), s.method, s.endpoint)
                  for s in stmts if s.semanticType == "api_call")


def test_proven_clients_are_api_calls(analyze_files) -> None:
    got = _api(analyze_files, (
        "    public function __construct(private HttpClientInterface $http) {}\n"
        "    public function f(Client $guzzle, ClientInterface $psr) {\n"
        "        $psr->sendRequest($request);\n"
        "        Http::get('https://api.example.com/users');\n"
        "        Http::withToken('t')->post('https://api.example.com/pay', []);\n"
        "        $guzzle->get('https://api.example.com/orders');\n"
        "        $client = new Client();\n"
        "        $client->request('PUT', 'https://api.example.com/orders/1');\n"
        "        $this->http->request('GET', 'https://api.example.com/items');\n"
        "        $sf = HttpClient::create();\n"
        "        $sf->request('DELETE', $url);\n"
        "        $pending = Http::acceptJson();\n"
        "        $pending->patch('https://api.example.com/me');\n"
        "    }\n"
    ))
    assert got == [
        ("$client->request('PUT', 'https://api.example.com/orders/1')", "PUT", "https://api.example.com/orders/1"),
        ("$guzzle->get('https://api.example.com/orders')", "GET", "https://api.example.com/orders"),
        ("$pending->patch('https://api.example.com/me')", "PATCH", "https://api.example.com/me"),
        ("$psr->sendRequest($request)", None, None),
        ("$sf->request('DELETE', $url)", "DELETE", None),
        ("$this->http->request('GET', 'https://api.example.com/items')", "GET", "https://api.example.com/items"),
        ("Http::get('https://api.example.com/users')", "GET", "https://api.example.com/users"),
        ("Http::withToken('t')->post('https://api.example.com/pay', [])", "POST", "https://api.example.com/pay"),
    ]


def test_names_alone_are_not_http_clients(analyze_files) -> None:
    got = _api(analyze_files, (
        "    public function f() {\n"
        "        $this->httpCache->get('user:1');\n"
        "        $this->httpCache->delete('user:1');\n"
        "        $httpStatusMap->get(404);\n"
        "        $this->apiClient->post('/v1/pay');\n"
        "        $stmt->fetch();\n"
        "        Http::fake();\n"
        "        $client = new Client();\n"
        "        $client->getConfig('base_uri');\n"
        "    }\n"
    ))
    assert got == []


def test_facade_needs_the_import(analyze_files) -> None:
    stmts = analyze_files({"app/T.php": "<?php\nnamespace App;\nclass T { function f() { Http::get('/x'); } }\n"})
    assert [s for s in stmts if s.semanticType == "api_call"] == []
