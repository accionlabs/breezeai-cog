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
import akka.actor.ActorRef

class Worker(ref: ActorRef) {
  def receive: Receive = {
    case x => println(x)
  }
  def run(): Unit = {
    ref ! msg
    ref ? msg
    val c = a ! b
    val d = cond ? a
    if (!flag) println("no")
  }
}
'''

PEKKO_SRC = b'''package workers
import org.apache.pekko.actor.typed.ActorRef
import org.apache.pekko.actor.typed.scaladsl.Behaviors

class Worker(ref: ActorRef[Cmd]) {
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


def _eventbus(rec: FileRecord) -> list:
    return [s for s in rec.statements if s.semanticType and s.semanticType.startswith("eventbus")]


def test_actor_sends_and_receive_are_not_entry_points(tmp_path: Path) -> None:
    # Decision Q7: a local actor message comes from our own code — `ref ! msg`, `ref ? msg` are
    # not outbound and `receive` is not an entry point. The file is still labelled akka.
    rec = _parse(tmp_path, AKKA_SRC)
    assert _eventbus(rec) == []
    assert rec.framework == "akka"


def test_byte_guard_suppresses_non_akka_files(tmp_path: Path) -> None:
    rec = _parse(tmp_path, NO_EVENTS_SRC, "Plain.scala")
    events = [s for s in rec.statements if s.semanticType and s.semanticType.startswith("eventbus")]
    assert events == []
    assert rec.framework is None


def test_pekko_only_file_detected_no_akka_substring(tmp_path: Path) -> None:
    # verified fact F: `org.apache.pekko` does not contain the substring "akka" —
    # the byte guard must check both spellings (the file is labelled; no actor records).
    assert b"akka" not in PEKKO_SRC
    rec = _parse(tmp_path, PEKKO_SRC, "Worker.scala")
    assert _eventbus(rec) == []
    assert rec.framework == "akka"


def test_play_controller_labels_play_and_actor_sends_stay_plain(tmp_path: Path) -> None:
    p = tmp_path / "NotifyController.scala"
    p.write_bytes(PLAY_AKKA_SRC)
    ctx = ParseContext(path="NotifyController.scala", abs_path=p, source=PLAY_AKKA_SRC,
                        repo_root=tmp_path, capture_statements=True)
    rec = ScalaPlayParser().parse_file(ctx)
    semantics = {s.semanticType for s in rec.statements}
    # The controller Action is the handler of a conf/routes binding, not an entry point itself.
    assert "route" not in semantics
    assert rec.framework == "play"
    assert "eventbus_send" not in semantics  # a local actor send is not outbound (Q7)


def test_events_output_validates(tmp_path: Path) -> None:
    rec = _parse(tmp_path, AKKA_SRC)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


IDIOMATIC_AKKA_SRC = b"""package com.example
import akka.actor._

class MyActor extends Actor {
  def receive: Receive = {
    case "start" =>
      val child = context.actorOf(Props[Worker])
      child ! "work"
      sender() ! "done"
      self ! "loop"
  }
}
"""


def test_idiomatic_akka_receivers(tmp_path: Path) -> None:
    rec = _parse(tmp_path, IDIOMATIC_AKKA_SRC, "MyActor.scala")
    assert _eventbus(rec) == []  # child / sender() / self sends are local (Q7)
    assert rec.framework == "akka"
