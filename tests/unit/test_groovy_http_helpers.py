"""Groovy HTTP calls made through the application's own helpers are recorded where they run
(``parsers/groovy_http_helpers.py``; contract §5 "Where an outbound record is written")."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog import iter_file_records

P = "src/main/groovy/com/acme/"
REST = """package com.acme

class OrderApi {
  static String BASE_URL = ""

  static Map getJson(String path) {
    String uri = BASE_URL + path
    log.info("[uri]:" + uri)
    return new OrderApiClient(uri).get()
  }

  static Map putJson(String path, Map body) {
    String uri = BASE_URL + path
    OrderApiClient client = new OrderApiClient(uri)
    return client.put(body)
  }

  static Map health() {
    return new OrderApiClient(BASE_URL + "/health").get()
  }

  static Map ordersOf(String id) {
    return new OrderApiClient(BASE_URL + "/" + id + "/orders").get()
  }

  Map fetch(String path) {
    return getJson(path)
  }
}
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


def test_helper_call_sites_are_the_requests(api_calls) -> None:
    got = api_calls({P + "OrderApi.groovy": REST, P + "Orders.groovy": """package com.acme

class Orders {
  static final String TABLE = "ORDERS"

  Map one(String id) {
    return OrderApi.getJson("/${TABLE}/${id}")
  }

  void save(String id, Map order) {
    String uri = "/" + TABLE + "/" + id
    OrderApi.putJson(uri, order)
  }
}
"""})
    assert got == [
        # the helpers' own requests are plain statements; these two are no helpers (the URL is
        # not the parameter's) and keep their record, as before
        ("OrderApi.groovy", 19, "GET", None),
        ("OrderApi.groovy", 23, "GET", None),
        ("Orders.groovy", 7, "GET", "${BASE_URL}/{TABLE}/{id}"),
        ("Orders.groovy", 12, "PUT", "${BASE_URL}/{TABLE}/{id}"),
    ]


def test_relay_and_unresolved_receiver_are_not_followed(api_calls) -> None:
    # `fetch(path)` passes its own parameter on (a relay, not followed): no record anywhere for
    # it; a `map.getJson(…)` beside an unresolved call of that name is not the helper's.
    got = api_calls({P + "OrderApi.groovy": REST, P + "Caller.groovy": """package com.acme

class Caller {
  void run(OrderApi rc, def other) {
    rc.fetch("/x")
    other.getJson("/y")
  }
}
"""})
    assert got == [("OrderApi.groovy", 19, "GET", None), ("OrderApi.groovy", 23, "GET", None)]


def test_helper_with_no_caller_has_no_record(api_calls) -> None:
    got = api_calls({P + "OrderApi.groovy": REST})
    assert ("OrderApi.groovy", 9, "GET", None) not in got
    assert ("OrderApi.groovy", 15, "PUT", None) not in got


def test_url_parameter_rewritten_or_named_in_the_call_is_no_helper(api_calls) -> None:
    got = api_calls({P + "Api.groovy": """package com.acme

class Api {
  static Map rewritten(String path) {
    path = "/v2" + path
    return new OrderApiClient(path).get()
  }

  static Map named(String base) {
    return new OrderApiClient(base).get(path: "/orders")
  }

  void run() { rewritten("/a"); named("/b") }
}
"""})
    # neither method is a helper: its own request keeps its record, its callers get none
    assert [g[:3] for g in got] == [("Api.groovy", 6, "GET"), ("Api.groovy", 10, "GET")]


def test_url_argument_read_at_runtime_is_named_after_its_value(api_calls) -> None:
    got = api_calls({P + "OrderApi.groovy": REST, P + "Tags.groovy": """package com.acme

class Tags {
  Map byBuilder(UriBuilder uri) {
    return OrderApi.getJson(uri.toString())
  }

  Map byIndex(List<String> paths) {
    return OrderApi.getJson(paths[0])
  }
}
"""})
    # a conversion takes its value's name; a value with no name gives no record
    assert ("Tags.groovy", 5, "GET", "${BASE_URL}${uri}") in got
    assert not [g for g in got if g[1] == 9 and g[0] == "Tags.groovy"]
