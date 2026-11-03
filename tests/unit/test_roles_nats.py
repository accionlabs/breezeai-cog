"""NATS (Go) role table (docs/entry-point-roles/nats.md): one test per row."""

from __future__ import annotations

MOD = "module example.com/app\n\ngo 1.22\n"
HEAD = 'package main\n\nimport "github.com/nats-io/nats.go"\n\n'


def _records(analyze_files, body: str):
    st = analyze_files({"go.mod": MOD, "main.go": HEAD + body})
    return {(s.semanticType, s.method, s.endpoint, s.handler, s.handlerLine)
            for s in st if s.framework == "nats"}


def test_subscribe_with_a_handler(analyze_files) -> None:
    assert _records(analyze_files, 'func main() {\n\tnc, _ := nats.Connect(nats.DefaultURL)\n'
                                   '\tnc.Subscribe("orders.created", onOrder)\n}\n\nfunc onOrder(m *nats.Msg) {}\n') \
        == {("eventbus_consumer", "MESSAGE", "orders.created", "onOrder", 10)}


def test_queue_subscribe(analyze_files) -> None:
    assert _records(analyze_files, 'func run(nc *nats.Conn) {\n\tnc.QueueSubscribe("orders.*", "workers",\n'
                                   '\t\tfunc(m *nats.Msg) {})\n}\n') \
        == {("eventbus_consumer", "MESSAGE", "orders.*", None, 7)}


def test_sync_subscription_is_handled_by_its_function(analyze_files) -> None:
    assert _records(analyze_files, 'func audit(nc *nats.Conn) {\n\tsub, _ := nc.SubscribeSync("audit")\n\t_ = sub\n}\n') \
        == {("eventbus_consumer", "MESSAGE", "audit", "audit", 5)}


def test_publish_and_publish_msg_and_jetstream(analyze_files) -> None:
    assert _records(analyze_files, 'func main() {\n\tnc, _ := nats.Connect("x")\n\tnc.Publish("a", nil)\n'
                                   '\tnc.PublishMsg(&nats.Msg{Subject: "b"})\n\tjs, _ := nc.JetStream()\n\tjs.Publish("c", nil)\n}\n') \
        == {("eventbus_publish", None, "a", None, None), ("eventbus_publish", None, "b", None, None),
            ("eventbus_publish", None, "c", None, None)}


def test_request_is_a_send(analyze_files) -> None:
    assert _records(analyze_files, 'func ask(nc *nats.Conn) {\n\tnc.Request("pricing.quote", nil, 0)\n}\n') \
        == {("eventbus_send", None, "pricing.quote", None, None)}


def test_subject_from_a_variable_has_no_endpoint(analyze_files) -> None:
    assert _records(analyze_files, 'func run(nc *nats.Conn, subj string) {\n\tnc.Publish(subj, nil)\n}\n') \
        == {("eventbus_publish", None, None, None, None)}


def test_value_that_is_not_a_connection(analyze_files) -> None:
    assert _records(analyze_files, 'func run(client *Client) {\n\tclient.Subscribe("x", h)\n\tclient.Publish("y", nil)\n}\n') == set()
