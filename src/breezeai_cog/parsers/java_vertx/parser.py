"""VertxParser — a Java framework parser. Selected (one parser per file) over JavaParser
when ``claims`` finds a Vert.x import; reuses ``JavaParser`` whole (single parse).

It is intentionally empty: it only owns the files no other Java framework claims, and its
output is plain ``JavaParser`` output. Do not add detection here — it would be lost on every
Vert.x file another framework owns.

The Vert.x event/messaging/route detection itself is the additive ``vertx`` detector
(``detector.py``), so it also runs when another Java framework parser owns the file (a Spring
``@Component`` calling ``vertx.setPeriodic``). Like all route/event detection, it is gated by
statement capture.

Both Vert.x package roots are recognised: ``io.vertx`` (Vert.x 3.x+) and ``org.vertx.java``
(Vert.x 2.x). Without the 2.x root a 2.x verticle fell through to the plain ``JavaParser``
and its EventBus wiring was never detected (``framework=null``)."""

from __future__ import annotations

from ..java.parser import JavaParser


class VertxParser(JavaParser):
    name = "java-vertx"
    # Below the other Java framework parsers (10): a file that is also a Spring / JAX-RS class
    # is theirs, and its Vert.x records still come from the additive `vertx` detector.
    priority = 5
    frameworks = ["vertx"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"io.vertx" in source or b"org.vertx" in source
