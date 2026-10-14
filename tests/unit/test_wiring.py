"""Shared wiring rules (parsers/wiring.py)."""

from __future__ import annotations

from breezeai_cog.parsers.wiring import Wiring, compose_guards, resolve_wiring


def _prefix(scope: str, value: str | None, site: str = "app.js:1") -> Wiring:
    return Wiring((scope,), "prefix", value, site)


def test_one_value_per_scope() -> None:
    items = [
        _prefix("users.js", "/users"),
        _prefix("users.js", "/users", "admin.js:4"),     # the same base twice: kept
        _prefix("orders.js", "/orders"),
        _prefix("orders.js", "/v2/orders", "v2.js:3"),   # two bases: ambiguous, dropped
        _prefix("cfg.js", None),                         # exists but unresolved: dropped
    ]
    assert resolve_wiring(items) == {("users.js",): "/users"}
    assert resolve_wiring(reversed(items)) == resolve_wiring(items)


def test_guards_outermost_first_without_repeats() -> None:
    assert compose_guards(["helmet", "auth"], None, ["auth", "admin"]) == ["helmet", "auth", "admin"]
    assert compose_guards(None, []) is None
