# Akka / Pekko actors — entry-point roles

Detector: `src/breezeai_cog/parsers/scala/events.py` (additive detector `akka-events`) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_akka.py`

| Construct | Role | Record | Why |
|---|---|---|---|
| `def receive: Receive = { case … }` in an actor | None | — | An in-process mailbox reached through an `ActorRef`; the source gives it no address the outside world uses (§3 check 2) |
| `Behaviors.receiveMessage { case … }` (typed actors) | None | — | Same |
| `ref ! msg` / `ref ? msg` | Outbound | `eventbus_send` | Our code sending to an actor |
| `system.actorOf(...)` / `context.spawn(...)` | None | — | Start-up |
