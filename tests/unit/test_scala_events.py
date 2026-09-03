"""Akka / Pekko messaging detection: ``ref ! msg`` (tell), ``ref ? msg`` (ask),
``receive``/``Behaviors.receiveMessage`` consumers. See
Spec/220/plan-P2-play-akka-BREEZEAI-220.md."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.scala.parser import ScalaParser
from breezeai_cog.parsers.scala_play.parser import ScalaPlayParser
from breezeai_cog.schemas import FileRecord

AKKA_SRC = b'''package workers
import akka.actor.Actor

class Worker extends Actor {
  def receive: Receive = {
    case x => println(x)
  }
  def run(): Unit = {
    ref ! msg
    ref ? msg
    if (!flag) println("no")
  }
}
'''

PEKKO_SRC = b'''package workers
import org.apache.pekko.actor.typed.scaladsl.Behaviors

class Worker {
  val b = Behaviors.receiveMessage[Cmd] { case x => Behaviors.same }
  def run(): Unit = {
    ref ! msg
  }
}
'''

NO_EVENTS_SRC = b'''package workers

class Plain {
  def run(): Unit = {
    ref ! msg
    if (!flag) println("no")
  }
}
'''

PLAY_AKKA_SRC = b'''package controllers
import play.api.mvc._
import akka.actor.ActorRef

class NotifyController(target: ActorRef) extends Controller {
  def notify = Action { implicit request =>
    target ! "ping"
    Ok("sent")
  }
}
'''


def _parse(tmp_path: Path, src: bytes, name: str = "Worker.scala") -> FileRecord:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(src)
    ctx = ParseContext(path=name, abs_path=p, source=src, repo_root=tmp_path,
                        capture_statements=True)
    return ScalaParser().parse_file(ctx)


def test_tell_and_ask_detected_unary_not_prefix(tmp_path: Path) -> None:
    rec = _parse(tmp_path, AKKA_SRC)
    sends = [s for s in rec.statements if s.semanticType == "eventbus_send"]
    methods = {s.method for s in sends}
    assert methods == {"SEND"}
    assert all(s.framework == "akka" for s in sends)
    # parented to the enclosing function run(), not the file
    run_fn = next(f for f in rec.functions if f.name == "run")
    assert all(s.parentId == run_fn.id for s in sends)
    # distinction stays in text
    texts = {s.text for s in sends}
    assert "ref ! msg" in texts
    assert "ref ? msg" in texts
    # unary `!flag` is a prefix_expression, structurally excluded — no extra send
    assert len(sends) == 2


def test_receive_handler_detected_as_consumer(tmp_path: Path) -> None:
    rec = _parse(tmp_path, AKKA_SRC)
    consumers = [s for s in rec.statements if s.semanticType == "eventbus_consumer"]
    assert len(consumers) == 1
    assert rec.framework == "akka"
    assert consumers[0].parentId != rec.id


def test_byte_guard_suppresses_non_akka_files(tmp_path: Path) -> None:
    rec = _parse(tmp_path, NO_EVENTS_SRC, "Plain.scala")
    events = [s for s in rec.statements if s.semanticType and s.semanticType.startswith("eventbus")]
    assert events == []
    assert rec.framework is None


def test_pekko_only_file_detected_no_akka_substring(tmp_path: Path) -> None:
    # verified fact F: `org.apache.pekko` does not contain the substring "akka" —
    # the byte guard must check both spellings.
    assert b"akka" not in PEKKO_SRC
    rec = _parse(tmp_path, PEKKO_SRC, "Worker.scala")
    semantics = {s.semanticType for s in rec.statements}
    assert "eventbus_send" in semantics
    assert "eventbus_consumer" in semantics  # Behaviors.receiveMessage
    sends = [s for s in rec.statements if s.semanticType == "eventbus_send"]
    assert all(s.method == "SEND" for s in sends)
    run_fn = next(f for f in rec.functions if f.name == "run")
    assert all(s.parentId == run_fn.id for s in sends)


def test_play_controller_emits_route_and_eventbus_send(tmp_path: Path) -> None:
    p = tmp_path / "NotifyController.scala"
    p.write_bytes(PLAY_AKKA_SRC)
    ctx = ParseContext(path="NotifyController.scala", abs_path=p, source=PLAY_AKKA_SRC,
                        repo_root=tmp_path, capture_statements=True)
    rec = ScalaPlayParser().parse_file(ctx)
    semantics = {s.semanticType for s in rec.statements}
    assert "route" in semantics
    assert "eventbus_send" in semantics


def test_events_output_validates(tmp_path: Path) -> None:
    rec = _parse(tmp_path, AKKA_SRC)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
