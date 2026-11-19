"""Celery role table: docs/entry-point-roles/celery.md — one test per row."""

from __future__ import annotations

EP = {"route", "eventbus_consumer", "timer"}

TASKS = """from celery import shared_task
from .celery import app

@shared_task
def cleanup(days):
    pass

@app.task(name="reports.send", bind=True)
def send_report(self):
    pass

@app.task
def ping():
    pass
"""
APP = """from celery import Celery
from celery.schedules import crontab
app = Celery("proj")
"""


def _repo(**extra: str) -> dict[str, str]:
    return {"proj/__init__.py": "", "proj/tasks.py": TASKS, "proj/celery.py": APP, **extra}


def _celery(stmts, path: str | None = None):
    return [s for s in stmts if s.framework == "celery" and (path is None or s.path == path)]


def _consumers(stmts) -> dict[str, str | None]:
    return {s.handler: s.endpoint for s in _celery(stmts) if s.semanticType == "eventbus_consumer"}


def test_task_is_binding_named_only_when_the_code_confirms_it(analyze_files) -> None:
    st = analyze_files(_repo(**{"proj/views.py": "from proj.celery import app\n"
                                                 "app.send_task('proj.tasks.cleanup')\n"}))
    # cleanup: its default name is written in send_task; send_report: name=; ping: nothing
    # confirms proj.tasks.ping (the worker's import path is not in the code) → empty.
    assert _consumers(st) == {"cleanup": "proj.tasks.cleanup", "send_report": "reports.send",
                              "ping": None}
    [r] = [s for s in _celery(st) if s.handler == "cleanup"]
    assert (r.semanticType, r.method, r.startLine, r.handlerLine, r.text) == (
        "eventbus_consumer", "MESSAGE", 4, 5, "@shared_task")


def test_task_module_confirmed_by_configuration(analyze_files) -> None:
    for config in ('app.autodiscover_tasks(["proj"])\n', 'app = Celery("p", include=["proj.tasks"])\n'):
        st = analyze_files(_repo(**{"proj/celery.py": APP + config}))
        assert _consumers(st)["ping"] == "proj.tasks.ping"


def test_beat_entry_is_a_timer_on_its_task(analyze_files) -> None:
    beat = APP + """from datetime import timedelta
app.conf.beat_schedule = {
    "nightly": {"task": "proj.tasks.cleanup", "schedule": crontab(hour=2, minute=0)},
    "often": {"task": "reports.send", "schedule": 30.0},
    "weekly": {"task": "proj.tasks.cleanup", "schedule": timedelta(days=7)},
    "gone": {"task": "elsewhere.task", "schedule": crontab(minute="*/15", day_of_week="mon-fri")},
}
"""
    st = analyze_files(_repo(**{"proj/celery.py": beat}))
    got = [(s.endpoint, s.handler) for s in _celery(st, "proj/celery.py")
           if s.semanticType == "timer"]
    assert got == [
        ("0 2 * * *", "cleanup"),
        ("30000", "send_report"),
        ("604800000", "cleanup"),
        ("*/15 * * * mon-fri", None),  # a task not in the repository: no handler
    ]
    # The beat entry confirms cleanup's default name.
    assert _consumers(st)["cleanup"] == "proj.tasks.cleanup"


def test_periodic_task_is_a_timer_on_its_task(analyze_files) -> None:
    setup = APP + """from proj.tasks import ping
@app.on_after_configure.connect
def setup(sender, **kwargs):
    sender.add_periodic_task(10.0, ping.s())
    sender.add_periodic_task(SOLAR, ping.s())
"""
    st = analyze_files(_repo(**{"proj/celery.py": setup}))
    got = [(s.method, s.endpoint, s.handler) for s in _celery(st, "proj/celery.py")]
    assert got == [("PERIODIC", "10000", "ping"), ("PERIODIC", None, "ping")]


def test_delay_and_apply_async_are_sends(analyze_files) -> None:
    views = """from proj.tasks import cleanup, ping as p
from proj import tasks
from proj.celery import app

def run():
    cleanup.delay(30)
    p.apply_async()
    tasks.send_report.delay()
    app.send_task("proj.tasks.cleanup")
"""
    st = analyze_files(_repo(**{"proj/views.py": views}))
    got = [(s.semanticType, s.endpoint) for s in _celery(st, "proj/views.py")]
    assert got == [
        ("eventbus_send", "proj.tasks.cleanup"),
        ("eventbus_send", None),            # ping's name is not confirmed
        ("eventbus_send", "reports.send"),  # `from proj import tasks` → proj/tasks.py
        ("eventbus_send", "proj.tasks.cleanup"),
    ]


def test_send_task_by_name_is_a_send(analyze_files) -> None:
    views = "from proj.celery import app\napp.send_task('a.b')\napp.send_task(name)\n"
    st = analyze_files(_repo(**{"proj/views.py": views}))
    assert [s.endpoint for s in _celery(st, "proj/views.py")] == ["a.b", None]


def test_configuration_has_no_record(analyze_files) -> None:
    st = analyze_files(_repo(**{"proj/celery.py": APP + 'app.autodiscover_tasks(["proj"])\n'}))
    assert _celery(st, "proj/celery.py") == []


def test_delay_on_something_else_has_no_record(analyze_files) -> None:
    views = "import time\ndef run(job):\n    job.delay(3)\n    time.delay()\n"
    st = analyze_files(_repo(**{"proj/views.py": views}))
    assert _celery(st, "proj/views.py") == []


# ── Only a Celery app's tasks and sends ───────────────────────────────────────────────────────
def test_send_task_is_a_send_only_on_a_celery_app(analyze_files) -> None:
    st = analyze_files(_repo(**{
        "proj/queue.py": "class JobQueue:\n    def send_task(self, name):\n        print(name)\n\n"
                         "queue = JobQueue()\nqueue.send_task('cleanup')\n",
        "proj/local.py": "from celery import Celery\napp = Celery('x')\napp.send_task('a.b')\n",
        "proj/imported.py": "from proj.celery import app as celery_app\ncelery_app.send_task('c.d')\n",
        "proj/current.py": "from celery import current_app\ncurrent_app.send_task('e.f')\n",
        "proj/dotted.py": "import celery\ncelery.current_app.send_task('g.h')\n",
    }))
    sends = {(s.path, s.endpoint) for s in _celery(st) if s.semanticType == "eventbus_send"}
    assert sends == {("proj/local.py", "a.b"), ("proj/imported.py", "c.d"),
                     ("proj/current.py", "e.f"), ("proj/dotted.py", "g.h")}


def test_dot_task_decorator_is_a_task_only_on_a_celery_app(analyze_files) -> None:
    st = analyze_files(_repo(**{"proj/jobs.py": (
        "import scheduler\nfrom celery import shared_task, current_app\n"
        "from proj.celery import app\n\n"
        "@scheduler.task\ndef tick():\n    pass\n\n"
        "@app.task(name='jobs.imported')\ndef imported():\n    pass\n\n"
        "@current_app.task(name='jobs.current')\ndef current():\n    pass\n\n"
        "@shared_task\ndef shared():\n    pass\n")}))
    got = {s.handler: s.endpoint for s in _celery(st, "proj/jobs.py")
           if s.semanticType == "eventbus_consumer"}
    assert got == {"imported": "jobs.imported", "current": "jobs.current", "shared": None}


def test_a_task_of_a_non_app_name_gives_no_record_even_when_the_project_uses_celery(
        analyze_files) -> None:
    st = analyze_files(_repo(**{"proj/other.py": (
        "import celery\nimport scheduler\n\n@scheduler.task\ndef tick():\n    pass\n")}))
    assert _celery(st, "proj/other.py") == []


def test_an_app_reached_through_its_module_is_a_celery_app(analyze_files) -> None:
    st = analyze_files(_repo(**{
        "proj/queue.py": "class JobQueue:\n    def send_task(self, name):\n        print(name)\n\n"
                         "queue = JobQueue()\n",
        "proj/plain.py": "import proj.celery\nproj.celery.app.send_task('a.b')\n",
        "proj/sub.py": "from proj import celery as worker\nworker.app.send_task('c.d')\n\n"
                       "@worker.app.task(name='sub.t')\ndef t():\n    pass\n",
        "proj/notapp.py": "import proj.queue\nproj.queue.queue.send_task('e.f')\n",
    }))
    sends = {(s.path, s.endpoint) for s in _celery(st) if s.semanticType == "eventbus_send"}
    assert sends == {("proj/plain.py", "a.b"), ("proj/sub.py", "c.d")}
    assert _consumers(st)["t"] == "sub.t"


# ── nodeType: the tree-sitter node each record sits at ────────────────────────────────────────
def test_records_take_the_tree_sitter_node_type(analyze_files) -> None:
    beat = APP + """from proj.tasks import ping
app.conf.beat_schedule = {"often": {"task": "reports.send", "schedule": 30.0}}
@app.on_after_configure.connect
def setup(sender, **kwargs):
    sender.add_periodic_task(10.0, ping.s())
"""
    st = analyze_files(_repo(**{"proj/celery.py": beat}))
    types = {(s.semanticType, s.path, s.nodeType) for s in _celery(st)}
    assert ("eventbus_consumer", "proj/tasks.py", "decorator") in types
    assert {(s.endpoint, s.nodeType) for s in _celery(st, "proj/celery.py")
            if s.semanticType == "timer"} == {("30000", "pair"), ("10000", "call")}
    assert not any(s.nodeType == "synthetic" for s in _celery(st))


# ── Beat entries added by subscript or update ─────────────────────────────────────────────────
def test_beat_entry_added_by_subscript_or_update_is_a_timer(analyze_files) -> None:
    beat = APP + """from datetime import timedelta
app.conf.beat_schedule["nightly"] = {"task": "proj.tasks.cleanup", "schedule": crontab(hour=2, minute=0)}
app.conf.beat_schedule.update({"weekly": {"task": "reports.send", "schedule": crontab(day_of_week=1)}})
app.conf.beat_schedule.update(hourly={"task": "reports.send", "schedule": timedelta(hours=1)})
app.conf.beat_schedule.update(EXTRA)
"""
    st = analyze_files(_repo(**{"proj/celery.py": beat}))
    got = [(s.endpoint, s.handler, s.nodeType) for s in _celery(st, "proj/celery.py")
           if s.semanticType == "timer"]
    assert got == [
        ("0 2 * * *", "cleanup", "assignment"),
        ("* * * * 1", "send_report", "pair"),
        ("3600000", "send_report", "keyword_argument"),
    ]
    assert _consumers(st)["cleanup"] == "proj.tasks.cleanup"  # the subscript entry confirms it


# ── Class tasks ───────────────────────────────────────────────────────────────────────────────
REPORT = """from celery import Task
from proj.celery import app

class Report(Task):
    name = "proj.report"

    def run(self, *args):
        pass

app.register_task(Report())
"""


def test_registered_class_task_is_a_consumer(analyze_files) -> None:
    beat = APP + ('app.conf.beat_schedule.update({"weekly": {"task": "proj.report", '
                  '"schedule": crontab(day_of_week=1)}})\n')
    st = analyze_files(_repo(**{"proj/report.py": REPORT, "proj/celery.py": beat}))
    [r] = [s for s in _celery(st, "proj/report.py") if s.semanticType == "eventbus_consumer"]
    assert (r.endpoint, r.handler, r.handlerLine, r.method, r.nodeType, r.startLine) == (
        "proj.report", "run", 7, "MESSAGE", "class_definition", 4)
    [t] = [s for s in _celery(st, "proj/celery.py") if s.semanticType == "timer"]
    assert (t.endpoint, t.handler) == ("* * * * 1", "run")


def test_class_task_registered_in_another_file(analyze_files) -> None:
    report = "from celery import Task\n\nclass Report(Task):\n    def run(self):\n        pass\n"
    worker = APP + "from proj.report import Report\napp.register_task(Report)\n"
    st = analyze_files(_repo(**{"proj/report.py": report, "proj/celery.py": worker,
                                "proj/views.py": "from proj.celery import app\n"
                                                 "app.send_task('proj.report.Report')\n"}))
    [r] = [s for s in _celery(st, "proj/report.py")]
    # No name= — Celery's default <module>.<Class> is confirmed by the send_task literal.
    assert (r.endpoint, r.handler) == ("proj.report.Report", "run")


def test_non_celery_or_unregistered_task_class_is_none(analyze_files) -> None:
    st = analyze_files(_repo(**{
        # A Task base that is not celery's.
        "proj/a.py": REPORT.replace("from celery import Task", "from myjobs import Task"),
        # Celery's Task, never registered.
        "proj/b.py": "from celery import Task\n\nclass B(Task):\n    name = 'b'\n"
                     "    def run(self):\n        pass\n",
        # Registered on something that is not a Celery app.
        "proj/c.py": "from celery import Task\nimport registry\n\nclass C(Task):\n"
                     "    def run(self):\n        pass\nregistry.register_task(C())\n",
        # A base of the project's own: inheritance is not followed.
        "proj/d.py": "from celery import Task\nfrom proj.celery import app\n"
                     "class Base(Task):\n    pass\nclass D(Base):\n    name = 'd'\n"
                     "    def run(self):\n        pass\napp.register_task(D())\n",
    }))
    assert [s for s in _celery(st) if s.path in ("proj/a.py", "proj/b.py", "proj/c.py", "proj/d.py")
            and s.semanticType == "eventbus_consumer" and s.handler == "run"] == []


# ── Canvas sends ──────────────────────────────────────────────────────────────────────────────
def test_signature_run_is_a_send_one_per_task(analyze_files) -> None:
    views = """from celery import chain, group, chord, signature
from proj.tasks import cleanup, ping, send_report

def go(ids):
    cleanup.s(30).delay()
    chain(cleanup.s(1), ping.si())()
    group([ping.s(i) for i in ids]).apply_async()
    (cleanup.s(2) | send_report.si()).delay()
    chord([ping.s()])(send_report.s())
    cleanup.apply_async((3,), link=send_report.s())
    signature("reports.send").apply_async()
"""
    st = analyze_files(_repo(**{"proj/views.py": views}))
    got = sorted((s.startLine, s.endpoint or "", s.nodeType) for s in _celery(st, "proj/views.py"))
    assert got == [
        (5, "", "call"),                          # cleanup's name is not confirmed here
        (6, "", "call"), (6, "", "call"),         # chain: cleanup, ping
        (7, "", "call"),                          # group: ping
        (8, "", "call"), (8, "reports.send", "call"),
        (9, "", "call"), (9, "reports.send", "call"),    # chord header, then body
        (10, "", "call"), (10, "reports.send", "call"),  # the task, then its link
        (11, "reports.send", "call"),
    ]


def test_signature_not_run_is_none(analyze_files) -> None:
    views = """from itertools import chain
from celery import group
from proj.tasks import cleanup, ping

def go():
    flow = group(cleanup.s(), ping.s())
    flow.delay()
    sig = cleanup.s()
    sig.delay()
    cleanup.s()()
    list(chain([cleanup.s()], [ping.s()]))
"""
    st = analyze_files(_repo(**{"proj/views.py": views}))
    assert _celery(st, "proj/views.py") == []
