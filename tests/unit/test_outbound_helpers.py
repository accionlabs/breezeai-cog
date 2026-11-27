"""The language-free part of following an application's own HTTP helpers
(``parsers/outbound_helpers.py``; contract §5 "Where an outbound record is written").

The engine sees plain values only, so its cases are data: each language's adapter tests check
the grammar side (source → these values)."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog import iter_file_records
from breezeai_cog.parsers.outbound_helpers import (
    CLEARED,
    Helper,
    HelperPolicy,
    Pass,
    RelayCall,
    Request,
    RequestSource,
    clear_emits,
    derive,
    enclosing_function,
    file_requests,
    follow_relays,
    link_site,
    merge_helpers,
    register_requests,
    request_sources,
    site_emit,
    site_verb,
    unresolved_twin,
    url_slot,
    verb_literal,
)
from breezeai_cog.parsers.treesitter import parse_source
from breezeai_cog.schemas import Call

VERBS = frozenset({"GET", "POST", "PUT"})


def fold(text: str) -> str | None:
    """A literal already read by the adapter (Python / TypeScript style)."""
    return text if text in VERBS else None


# ── url_slot ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("refs", "template", "slot"), [
    (["path"], "{BASE}{path}", "{path}"),          # the URL's last part
    (["url"], "{url}", "{url}"),                   # the whole URL
    ([], "/health", None),                         # no parameter: an ordinary request
    (["a", "b"], "{a}{b}", None),                  # two parameters
    (["base"], "{base}/orders", None),             # a base: the function names its endpoint
    (["id"], "/orders/{id}/items", None),          # an id in the middle
    (["p"], "{p}/{p}", None),                      # the parameter twice
])
def test_url_slot(refs: list[str], template: str, slot: str | None) -> None:
    assert url_slot(refs, template) == slot


def test_merge_drops_a_key_declared_two_ways() -> None:
    a, b = Helper(0, "{u}", "{u}", "GET"), Helper(0, "{u}", "{u}", "POST")
    assert merge_helpers([("k1", a), ("k1", a), ("k2", a), ("k2", b), ("k3", b)]) == {
        "k1": a, "k3": b}


# ── derive: what a relay becomes ─────────────────────────────────────────────────────────────

HELPER = Helper(url_index=1, template="{apiPath}{url}", slot="{url}", verb=None, verb_index=0,
                url_name="url", verb_name="method", verb_default="get")


@pytest.mark.parametrize(("passes", "literals", "expect"), [
    # the parameter passed as it is (Java): the helper's slot kept; the verb passed on too
    ((Pass(1, 0), Pass(0, 1)), (),
     dict(url_index=0, template="{apiPath}{url}", slot="{url}", verb=None, verb_index=1)),
    # an expression holding the parameter: folded into the template, the slot renamed
    ((Pass(1, 0, "p", "/v1{p}"), Pass(0, 1, "m", "{m}")), (),
     dict(url_index=0, url_name="p", template="{apiPath}/v1{p}", slot="{p}", verb_index=1,
          verb_name="m")),
    # by keyword (Python): found by the parameter's name
    ((Pass("url", 2, "target", "{target}"),), (("method", "POST"),),
     dict(url_index=2, url_name="target", template="{apiPath}{target}", slot="{target}",
          verb="POST", verb_index=-1)),
    # the verb left out: the helper parameter's default
    ((Pass(1, 0, "u", "{u}"),), (),
     dict(url_index=0, url_name="u", template="{apiPath}{u}", slot="{u}", verb="GET",
          verb_index=-1)),
    # a constant that is no verb, or an expression in the verb position: unknown
    ((Pass(1, 0, "u", "{u}"),), ((0, "FETCH"),), dict(verb=None, verb_index=-1)),
    ((Pass(1, 0, "u", "{u}"), Pass(0, 1, "m", "{m}.lower()")), (), dict(verb=None, verb_index=-1)),
])
def test_derive(passes: tuple[Pass, ...], literals: tuple[tuple[int | str, str], ...],
                expect: dict[str, object]) -> None:
    got = derive(HELPER, RelayCall("r", None, passes, literals), fold_verb=fold)
    assert got is not None
    for name, value in expect.items():
        assert getattr(got, name) == value, name
    assert got.verb_default is None


def test_derive_without_the_url_passed_is_nothing() -> None:
    assert derive(HELPER, RelayCall("r", None, (Pass(0, 0, "m", "{m}"),)), fold_verb=fold) is None


def test_derive_keeps_the_framework_and_takes_the_relay_kind() -> None:
    helper = Helper(0, "{u}", "{u}", "GET", framework="axios")
    got = derive(helper, RelayCall("r", None, (Pass(0, 0, "x", "{x}"),), is_method=True),
                 fold_verb=fold)
    assert got is not None and got.framework == "axios" and got.is_method


# ── follow_relays ────────────────────────────────────────────────────────────────────────────

def _chain(n: int) -> list[RelayCall[str]]:
    """r1 → helper, r2 → r1, …, rn → r(n-1): each passes its parameter 0 as it is."""
    return [RelayCall(f"r{i}", f"r{i - 1}" if i > 1 else "helper", (Pass(0, 0),))
            for i in range(1, n + 1)]


def _by_name(known: dict[str, Helper]):
    return lambda relay: known.get(relay.target)


@pytest.mark.parametrize(("relays", "depth", "found"), [
    ("same-file", 4, {"helper", "r1", "r2", "r3", "r4"}),
    ("same-file", 2, {"helper", "r1", "r2"}),           # the depth bounds the chain
    ("none", 4, {"helper"}),                            # Groovy today: no relay followed
])
def test_follow_relays_depth(relays: str, depth: int, found: set[str]) -> None:
    known = {"helper": Helper(0, "{u}", "{u}", "GET")}
    policy = HelperPolicy("x", relays=relays, depth=depth)  # type: ignore[arg-type]
    follow_relays(known, _chain(6), lookup=_by_name(known), fold_verb=fold, policy=policy)
    assert set(known) == found


def test_blocked_keys_are_never_derived() -> None:
    known = {"helper": Helper(0, "{u}", "{u}", "GET")}
    follow_relays(known, _chain(3), lookup=_by_name(known), fold_verb=fold,
                  policy=HelperPolicy("x"), blocked={"r2"})
    assert set(known) == {"helper", "r1"}  # r3 calls r2, which is never known


def _ambiguous_then_clear() -> tuple[dict[str, Helper], list[RelayCall[str]]]:
    """``r`` calls ``a`` and ``b`` with its parameter: round 1 knows only ``a`` and ``b0``, whose
    requests differ (``b`` is derived from ``b0`` in round 1 too) — ``r`` agrees with itself only
    once ``b`` is known and equal to ``a``… here it never is, so ``r`` stays ambiguous; ``s``
    calls ``r``."""
    known = {"a": Helper(0, "{u}", "{u}", "GET"), "b0": Helper(0, "{u}", "{u}", "POST")}
    relays = [RelayCall("r", "a", (Pass(0, 0),)), RelayCall("r", "b0", (Pass(0, 0),)),
              RelayCall("s", "r", (Pass(0, 0),))]
    return known, relays


@pytest.mark.parametrize("sticky", [True, False])
def test_an_ambiguous_relay_is_not_known(sticky: bool) -> None:
    known, relays = _ambiguous_then_clear()
    blocked: set[str] = set()
    follow_relays(known, relays, lookup=_by_name(known), fold_verb=fold,
                  policy=HelperPolicy("x", sticky_ambiguous=sticky), blocked=blocked)
    assert "r" not in known and "s" not in known
    assert ("r" in blocked) is sticky  # Java drops it for good; Python and TypeScript retry


def test_on_round_gets_the_new_helpers_and_their_files() -> None:
    known = {"helper": Helper(0, "{u}", "{u}", "GET")}
    relays = [RelayCall("r1", "helper", (Pass(0, 0),), origin="a.java"),
              RelayCall("r2", "r1", (Pass(0, 0),), origin="b.java")]
    rounds: list[tuple[set[str], dict[str, str | None]]] = []
    follow_relays(known, relays, lookup=_by_name(known), fold_verb=fold,
                  policy=HelperPolicy("x", relays="cross-file", depth=6),
                  on_round=lambda new, origins: rounds.append((set(new), origins)))
    assert rounds == [({"r1"}, {"r1": "a.java"}), ({"r2"}, {"r2": "b.java"})]


def test_follow_relays_is_linear_in_the_relays() -> None:
    """Cost guard (§11 of the architecture): one pass over the relays per round."""
    looked: list[int] = []
    known = {"helper": Helper(0, "{u}", "{u}", "GET")}
    relays = [RelayCall(f"r{i}", "helper", (Pass(0, 0),)) for i in range(5000)]

    def lookup(relay: RelayCall[str]) -> Helper | None:
        looked.append(1)
        return known.get(relay.target)

    follow_relays(known, relays, lookup=lookup, fold_verb=fold, policy=HelperPolicy("x"))
    assert len(known) == 5001
    assert len(looked) == 5000  # round 2 finds them all known and stops


# ── Call sites ───────────────────────────────────────────────────────────────────────────────

SITES = {("api.py", "get_json"): "K1", ("other.py", "get_json"): "K2"}


@pytest.mark.parametrize(("calls", "key"), [
    ([Call(name="get_json", path="api.py")], "K1"),
    ([Call(name="get_json", path="api.py"), Call(name="other")], "K1"),
    ([Call(name="get_json", path="api.py"), Call(name="get_json", path="other.py")], None),
    ([Call(name="get_json")], None),                        # resolves nowhere
    ([Call(name="get_json", path="lib.py")], None),         # resolves to no helper
])
def test_link_site(calls: list[Call], key: str | None) -> None:
    assert link_site(calls, "get_json", lambda path: SITES.get((path, "get_json"))) == key


def test_unresolved_twin() -> None:
    assert unresolved_twin([Call(name="get", path="a.py"), Call(name="get")], "get")
    assert not unresolved_twin([Call(name="get", path="a.py"), Call(name="put")], "get")


@pytest.mark.parametrize(("helper", "literal", "verb"), [
    (Helper(0, "{u}", "{u}", "GET"), "POST", "GET"),            # fixed by the helper
    (Helper(0, "{u}", "{u}", None, verb_index=1), "post", "POST"),
    (Helper(0, "{u}", "{u}", None, verb_index=1), "FETCH", None),
    (Helper(0, "{u}", "{u}", None, verb_index=1), None, None),
])
def test_site_verb(helper: Helper, literal: str | None, verb: str | None) -> None:
    assert site_verb(helper, literal, VERBS) == verb


def test_verb_literal() -> None:
    assert verb_literal("put", VERBS) == "PUT" and verb_literal("", VERBS) is None


def _tree_and_record(tmp_path: Path, rel: str, src: str):
    repo = tmp_path / "repo"
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)
    record = next(r for r in iter_file_records(repo) if r.path == rel)
    return parse_source("python", src.encode()).root_node, record


def test_site_emit_and_clearing(tmp_path: Path) -> None:
    # a request on a fixed URL (no helper: the live Python rule keeps its record)
    src = "import requests\n\ndef health():\n    return requests.get('https://a.io/h').json()\n"
    root, record = _tree_and_record(tmp_path, "api.py", src)
    call = root.named_children[1].child_by_field_name("body").named_children[0].named_children[0]
    request = call.child_by_field_name("function").child_by_field_name("object")  # requests.get(…)

    emits = clear_emits([request], "api.py", record)
    assert len(emits) == 1 and emits[0].node.type == "return_statement"
    assert emits[0].fields == dict(CLEARED) and emits[0].enrich == "same"
    assert enclosing_function(record, 4) is not None and enclosing_function(record, 1) is None

    helper = Helper(0, "{BASE}{u}", "{u}", "GET", framework="axios")
    e = site_emit(request, helper, "/orders/{id}", "GET",
                  HelperPolicy("x", reclassify=frozenset({"api_call"})))
    assert e.fields == {"semanticType": "api_call", "method": "GET",
                        "endpoint": "{BASE}/orders/{id}", "framework": "axios"}
    assert e.enrich == "unclassified" and e.reclassify == frozenset({"api_call"})
    plain = site_emit(request, Helper(0, "{u}", "{u}"), "/x", None, HelperPolicy("x"))
    assert "framework" not in plain.fields


# ── Framework recognizers ────────────────────────────────────────────────────────────────────

def test_registered_recognizers_run_on_gated_files() -> None:
    seen: list[bytes] = []

    def recognize(root, source):  # type: ignore[no-untyped-def]
        seen.append(source)
        return [Request(node=root, url=None, verb="GET", framework="fake")]

    src = RequestSource("zz-test-fake", "test-lang", (b"FakeClient",), recognize)
    assert register_requests(src) is src
    assert register_requests(RequestSource("zz-test-fake", "test-lang", (b"x",), recognize)) is src
    assert [s.name for s in request_sources("test-lang")] == ["zz-test-fake"]

    root = parse_source("python", b"x = 1\n").root_node
    assert file_requests("test-lang", root, b"no client here") == []
    found = file_requests("test-lang", root, b"FakeClient(url).get()")
    assert [r.framework for r in found] == ["fake"] and len(seen) == 1
