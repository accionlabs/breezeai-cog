"""A base class that registers the object itself on the address its constructor was given, so a
subclass's ``super("addr")`` is the registration: the handler may be ``this``, a method
reference on ``this`` or a lambda (java_vertx/forwarders.py)."""

from __future__ import annotations

import pytest

P = "src/main/java/a/"


def _base(handler: str) -> str:
    return f"""package a;
import io.vertx.core.AbstractVerticle;
import io.vertx.core.Handler;
import io.vertx.core.eventbus.Message;
public abstract class Base extends AbstractVerticle implements Handler<Message<Object>> {{
  private final String address;
  protected Base(String address) {{ this.address = address; }}
  public void start() {{ vertx.eventBus().consumer(address, {handler}); }}
  public void handle(Message<Object> m) {{ onMessage(m); }}
  protected abstract void onMessage(Message<Object> m);
}}
"""


ORDERS = """package a;
import io.vertx.core.eventbus.Message;
public class Orders extends Base {
  public Orders() { super("orders.create"); }
  protected void onMessage(Message<Object> m) {}
}
"""


def _consumers(stmts):
    return sorted(
        (s.path.rsplit("/", 1)[-1], s.startLine, s.endpoint, s.handler, s.handlerLine, s.handlerPath)
        for s in stmts if s.semanticType == "eventbus_consumer")


@pytest.mark.parametrize(
    "handler, expected",
    [
        # the subclass's own override runs: its name and line
        ("this::onMessage", ("Orders.java", 4, "orders.create", "onMessage", 5, None)),
        # a lambda runs where it is written: its line in the base class, no name
        ("m -> onMessage(m)", ("Orders.java", 4, "orders.create", None, 8, P + "Base.java")),
        # the object itself: its `handle` (inherited here: not located, a subclass may override)
        ("this", ("Orders.java", 4, "orders.create", "handle", None, None)),
    ],
)
def test_super_call_is_the_consumer(analyze_files, handler, expected) -> None:
    got = _consumers(analyze_files({P + "Base.java": _base(handler), P + "Orders.java": ORDERS}))
    assert expected in got
    # the base's own registration, on the field its constructor sets, only defines it: each
    # subclass's super(…) is the registration — no record in the base (contract §5)
    assert not [c for c in got if c[0] == "Base.java"]


def test_an_inherited_method_reference_is_not_located(analyze_files) -> None:
    # `onMessage` is the base's (abstract) declaration here: a subclass of Plain may override it
    plain = """package a;
public class Plain extends Base {
  public Plain() { super("orders.plain"); }
}
"""
    got = _consumers(analyze_files({P + "Base.java": _base("this::onMessage"), P + "Plain.java": plain}))
    assert ("Plain.java", 3, "orders.plain", "onMessage", None, None) in got


def test_a_method_reference_on_another_object_is_not_self_registration(analyze_files) -> None:
    base = _base("this::onMessage").replace("this::onMessage", "router::onMessage")
    got = _consumers(analyze_files({P + "Base.java": base, P + "Orders.java": ORDERS}))
    assert [c for c in got if c[0] == "Orders.java"] == []
