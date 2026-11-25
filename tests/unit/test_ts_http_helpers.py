"""TypeScript / JavaScript HTTP calls made through the application's own helpers are recorded where
they run (BREEZEAI-1654, ``parsers/typescript_http_helpers.py``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog import iter_file_records

HTTP = """import axios from 'axios';
export async function getRequest(url: string, headers: Record<string, string>) {
  const response = await axios.get(url, { headers });
  return response.data;
}
export const postRequest = async (url: string, body: unknown) => {
  const response = await axios.post(url, body);
  return response.data;
};
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


def test_caller_is_the_request(api_calls) -> None:
    got = api_calls({"src/utils/http.ts": HTTP, "src/svc.ts": """import { getRequest, postRequest } from './utils/http';
export async function resolveField(domain: string) {
  const url = `https://${domain}.atlassian.net/rest/api/3/field`;
  await getRequest(url, {});
  await postRequest('/api/orders', {});
}
"""})
    assert got == [
        ("svc.ts", 4, "GET", "https://${domain}.atlassian.net/rest/api/3/field"),
        ("svc.ts", 5, "POST", "/api/orders"),
    ]


def test_helper_with_no_caller_has_no_record(api_calls) -> None:
    assert api_calls({"src/utils/http.ts": HTTP}) == []


def test_class_relay_with_verb_argument(api_calls) -> None:
    got = api_calls({"src/api.ts": """import axios from 'axios';
export class Api {
  request(method: string, url: string) { return axios.request(method, url); }
  get(url: string) { return this.request('GET', url); }
  del(url: string) { return this.request('DELETE', url); }
}
""", "src/use.ts": """import { Api } from './api';
export class Use {
  constructor(private api: Api) {}
  run() { this.api.get('/orders'); this.api.del('/orders/1'); }
}
"""})
    assert got == [("use.ts", 4, "DELETE", "/orders/1"), ("use.ts", 4, "GET", "/orders")]


def test_unproven_helpers_keep_their_record(api_calls) -> None:
    # two parameters in the URL; the parameter rewritten; a config object
    got = api_calls({"src/http.ts": """import axios from 'axios';
export function two(host: string, path: string) { return axios.get(host + path); }
export function rewritten(url: string) { url = url.trim(); return axios.get(url); }
export function config(url: string) { return axios({ url, method: 'get' }); }
""", "src/use.ts": """import { two, rewritten, config } from './http';
export function run() { two('h', '/p'); rewritten('/r'); config('/c'); }
"""})
    assert [(f, line) for f, line, _, _ in got] == [("http.ts", 2), ("http.ts", 3), ("http.ts", 4)]


def test_name_shared_by_two_functions_is_not_followed(api_calls) -> None:
    got = api_calls({"src/http.ts": """import axios from 'axios';
export class A { get(url: string) { return axios.get(url); } }
export class B { get(url: string) { return axios.get(url + '/b'); } }
""", "src/use.ts": """import { A } from './http';
export function run(a: A) { a.get('/x'); }
"""})
    assert [(f, line) for f, line, _, _ in got] == [("http.ts", 2), ("http.ts", 3)]


def test_object_literal_helper_and_template(api_calls) -> None:
    got = api_calls({"src/api.js": """const axios = require('axios');
const api = {
  fetchOrder(id) { return axios.get(`/api/orders/${id}`); },
};
module.exports = { api };
""", "src/use.js": """const { api } = require('./api');
function run() { return api.fetchOrder(42); }
module.exports = { run };
"""})
    assert got == [("use.js", 2, "GET", "/api/orders/42")]


def test_fetch_helper_takes_the_verb_from_its_init(api_calls) -> None:
    got = api_calls({"src/http.ts": """async function fetchRaw(url: string, method: string, body?: string) {
  return fetch(url, { method, body });
}
export async function get(path: string) { return fetchRaw('/api' + path, 'GET'); }
export async function post(path: string, body: string) { return fetchRaw('/api' + path, 'POST', body); }
export async function load(u: string) { return fetch(u); }
""", "src/use.ts": """import { get, post, load } from './http';
export async function run() { await get('/a'); await post('/b', 'x'); await load('/c'); }
"""})
    # get / post build their URL from their parameter: they relay it, and the caller is the request
    assert got == [("use.ts", 2, "GET", "/api/a"), ("use.ts", 2, "GET", "/c"),
                   ("use.ts", 2, "POST", "/api/b")]


def test_function_naming_its_own_endpoint_is_no_helper(api_calls) -> None:
    # a base or an id from a parameter: the function calls one endpoint, and keeps its record
    got = api_calls({"src/orders.ts": """import axios from 'axios';
export function load(apiUrl: string) { return axios.get(`${apiUrl}/orders`); }
export function items(id: string) { return axios.get(`/api/orders/${id}/items`); }
"""})
    assert got == [("orders.ts", 2, "GET", "${apiUrl}/orders"), ("orders.ts", 3, "GET", "/api/orders/{id}/items")]


def test_same_name_on_a_map_is_not_the_helper(api_calls) -> None:
    got = api_calls({"src/api.ts": """import axios from 'axios';
export class Api {
  get(url: string) { return axios.get(url); }
  run(cache: Map<string, string>) { const k = cache.get('key'); return this.get('/orders'); }
}
"""})
    assert got == [("api.ts", 4, "GET", "/orders")]


def _frameworks(tmp_path: Path, files: dict[str, str]) -> dict[tuple[str, int], str | None]:
    repo = tmp_path / "fw"
    for rel, src in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    return {(r.path.rsplit("/", 1)[-1], s.startLine): s.framework
            for r in iter_file_records(repo) for s in r.statements if s.semanticType == "api_call"}


def test_call_site_carries_the_helpers_client_label(tmp_path: Path) -> None:
    got = _frameworks(tmp_path, {
        "http.ts": HTTP + """export async function raw(url: string) {
  return fetch(url);
}
export function viaApi(path: string) {
  return getRequest('/api' + path, {});
}
""",
        "client.ts": """export async function load(url: string) {
  return http.get(url);
}
""",
        "svc.ts": """import { getRequest, raw, viaApi } from './http';
import { load } from './client';
export async function run() {
  await getRequest('https://a.example/x', {});
  await raw('https://a.example/y');
  await viaApi('/orders');
  await load('https://a.example/z');
}
"""})
    assert got[("svc.ts", 4)] == "axios"
    assert got[("svc.ts", 5)] == "fetch"
    assert got[("svc.ts", 6)] == "axios"   # through a relay
    assert got[("svc.ts", 7)] is None      # `http` is no client the detectors vouch for


def test_a_spread_argument_makes_later_positions_unknown(api_calls) -> None:
    got = api_calls({"t.ts": """export async function request(method: string, url: string) {
  return fetch(url, { method });
}
""", "u.ts": """import { request } from './t';
export async function a(m: string[]) { await request(...m, 'https://x.example/c'); }
export async function b() { await request('POST', 'https://x.example/d'); }
"""})
    assert got == [("u.ts", 3, "POST", "https://x.example/d")]
