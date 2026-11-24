"""``framework`` on a generic outbound call names its HTTP client only from evidence in the file
(typescript_axios, typescript_fetch, typescript_angular/http_client.py); otherwise it stays
empty, and no statement is ever added."""

from __future__ import annotations


def _calls(stmts, path):
    return {s.startLine: s.framework for s in stmts
            if s.path == path and s.semanticType == "api_call"}


def test_axios(analyze_files) -> None:
    got = _calls(analyze_files({"src/a.ts": """import axios from 'axios';
const api = axios.create({ baseURL: '/api' });
export async function a() { return axios.get('/orders'); }
export async function b() { return api.post('/orders', {}); }
export async function c() { return Promise.all([axios.get('/x'), fetch('/y')]); }
export async function d(http) { return http.get('/z'); }
export async function e() { return axios({ url: '/w', method: 'post' }); }
"""}), "src/a.ts")
    assert got[3] == "axios"
    assert got[4] == "axios"  # an instance of the imported axios
    assert got[5] is None  # two clients in one statement
    assert got[6] is None  # no evidence
    assert got[7] == "axios"


def test_axios_required(analyze_files) -> None:
    got = _calls(analyze_files({"src/r.js": """const axios = require('axios');
module.exports = () => axios.get('/orders');
"""}), "src/r.js")
    assert got[2] == "axios"


def test_axios_shadowed_or_not_imported(analyze_files) -> None:
    got = _calls(analyze_files({
        "src/a.ts": """import axios from 'axios';
export async function a(axios) { return axios.get('/orders'); }
""",
        "src/b.ts": """import axios from './my-axios';
export async function a() { return axios.get('/orders'); }
""",
    }), "src/a.ts")
    assert got[2] is None  # a parameter of that name
    stmts = analyze_files({"src/b.ts": """import axios from './my-axios';
export async function a() { return axios.get('/orders'); }
"""})
    assert _calls(stmts, "src/b.ts")[2] is None  # the app's own module


def test_fetch(analyze_files) -> None:
    stmts = analyze_files({
        "src/f.ts": """export async function a() { return fetch('/orders'); }
export async function b() { return window.fetch('/x'); }
""",
        "src/g.ts": """import fetch from 'node-fetch';
export async function a() { return fetch('/orders'); }
""",
        "src/h.ts": """import { fetch } from './my-client';
export async function a() { return fetch('/orders'); }
""",
        "src/i.ts": """function fetch(u: string) { return u; }
export async function a() { return fetch('/orders'); }
""",
    })
    assert _calls(stmts, "src/f.ts") == {1: "fetch", 2: "fetch"}
    assert _calls(stmts, "src/g.ts")[2] == "fetch"  # a Fetch implementation
    assert _calls(stmts, "src/h.ts")[2] is None  # another module's `fetch`
    assert _calls(stmts, "src/i.ts")[2] is None  # a local `fetch`


def test_angular_http_client(analyze_files) -> None:
    got = _calls(analyze_files({"src/app/orders.service.ts": """import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
@Injectable({ providedIn: 'root' })
export class OrdersService {
  private client = inject(HttpClient);
  constructor(private http: HttpClient, private httpService: LegacyHttp) {}
  a() { return this.http.get('/api/orders'); }
  b() { return this.client.post('/api/orders', {}); }
  c() { return this.httpService.get('/api/legacy'); }
}
"""}), "src/app/orders.service.ts")
    assert got[7] == "angular"
    assert got.get(8) in (None, "angular")  # labelled only if the base records it
    assert got[9] is None  # another type


def test_angular_injected_local(analyze_files) -> None:
    got = _calls(analyze_files({"src/app/orders.store.ts": """import { inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
export function withOrders(apiUrl: string) {
  const http = inject(HttpClient);
  return () => http.get(`${apiUrl}/orders`);
}
export function other(http: Legacy) { return http.get('/legacy'); }
"""}), "src/app/orders.store.ts")
    assert got[5] is None  # `http` is also a parameter in this file: no certain binding
    assert got.get(7) is None  # a parameter of that name: the evidence is shadowed


def test_angular_injected_local_once(analyze_files) -> None:
    got = _calls(analyze_files({"src/app/orders.store.ts": """import { inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
export function load(apiUrl: string) {
  const http = inject(HttpClient);
  return http.get(`${apiUrl}/orders`);
}
"""}), "src/app/orders.store.ts")
    assert got[5] == "angular"


def _ids(stmts, path):
    return sorted(s.id.split(":", 1)[1] for s in stmts if s.path == path)


def test_no_statement_is_added(analyze_files) -> None:
    body = """const api = axios.create({});
export const load = () => api.get('/orders');
export const all = () => Promise.all([axios.get('/a'), axios.get('/b')]);
"""
    labelled = analyze_files({"a/src/a.ts": "import axios from 'axios';\n" + body})
    plain = analyze_files({"b/src/a.ts": "import axios from './own';\n" + body})
    assert _ids(labelled, "a/src/a.ts") == _ids(plain, "b/src/a.ts")
    assert "axios" in {s.framework for s in labelled if s.path == "a/src/a.ts"}
