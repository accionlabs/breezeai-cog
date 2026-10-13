"""Akka / Pekko actor role table: docs/entry-point-roles/akka.md — one test per row."""

from __future__ import annotations


EP = {"route", "eventbus_consumer", "timer"}
SRC = "src/main/scala/a/Worker.scala"


def _eps(stmts):
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


def test_actor_receive_is_none(analyze_files) -> None:
    st = analyze_files(
        {
            SRC: """package a
import akka.actor.Actor
class Worker extends Actor {
  def receive: Receive = {
    case "ping" => println("pong")
  }
}
"""
        }
    )
    assert _eps(st) == []


def test_typed_receive_message_is_none(analyze_files) -> None:
    st = analyze_files(
        {
            SRC: """package a
import akka.actor.typed.scaladsl.Behaviors
object Worker {
  def apply() = Behaviors.receiveMessage[String] {
    case "ping" => Behaviors.same
  }
}
"""
        }
    )
    assert _eps(st) == []


def test_tell_is_not_outbound(analyze_files) -> None:
    st = analyze_files(
        {
            SRC: """package a
import akka.actor.ActorRef
class Client(worker: ActorRef) {
  def ping(): Unit = worker ! "ping"
}
"""
        }
    )
    assert _eps(st) == []
    assert [s.semanticType for s in st if s.semanticType] == []  # local actor send (Q7)


def test_actor_creation_is_none(analyze_files) -> None:
    st = analyze_files(
        {
            SRC: """package a
import akka.actor.{ActorSystem, Props}
object Main {
  val system = ActorSystem("app")
  val worker = system.actorOf(Props[Worker](), "worker")
}
"""
        }
    )
    assert _eps(st) == []
