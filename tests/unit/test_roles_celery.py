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
