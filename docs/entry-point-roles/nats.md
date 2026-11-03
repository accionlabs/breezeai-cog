# NATS (Go) — entry-point roles

Parser: `src/breezeai_cog/parsers/go_nats/` (additive detector, Go files importing `github.com/nats-io/nats.go`) · Typed values: `src/breezeai_cog/parsers/go/values.py` · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_nats.py`

**What is a connection.** A value bound from `nats.Connect(…)` / `nats.NewEncodedConn(…)`, from
`nc.JetStream()`, or a parameter or struct field declared `*nats.Conn`, `*nats.EncodedConn` or
`nats.JetStreamContext` — never a variable's name.

| Construct | Role | Record | Why |
|---|---|---|---|
| `nc.Subscribe(subject, handler)` / `js.Subscribe(…)` | Binding | `eventbus_consumer`, `method: MESSAGE`, `framework: nats`, `endpoint` = the subject, `handler` = the callback (`handlerLine` = where a function literal starts) | One handler, inbound message |
| `nc.QueueSubscribe(subject, queue, handler)` | Binding | as above | Queue group of the same subject |
| `nc.SubscribeSync(subject)` / `ChanSubscribe(subject, ch)` / `js.PullSubscribe(…)` | Binding | as above; `handler` = the function the call is in | The function receives the messages |
| `nc.Publish(subject, data)` / `PublishMsg(&nats.Msg{Subject: …})` | Outbound | `eventbus_publish`, `endpoint` = the subject | Our code publishing |
| `nc.Request(subject, data, timeout)` | Outbound | `eventbus_send` | Request-reply |
| A subject from a variable | Binding / Outbound | `endpoint` empty | Never a guessed address |
| `client.Subscribe(…)` on a value that is not a NATS connection | None | — | Not bound from the package |
