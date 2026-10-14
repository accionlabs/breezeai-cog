# Celery — entry-point roles

Parser: `src/breezeai_cog/parsers/python/celery.py` (additive detector), `python/celery_tasks.py` (task names, a registered fact) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_celery.py`

A task is addressed by its name. `name=` gives it; otherwise Celery's default `<module>.<function>` is used **only when the code confirms it**: the name is written as a literal somewhere (a beat entry, `send_task("…")`, `signature("…")`), or the module is in Celery's task-module configuration (`autodiscover_tasks(["proj"])` → `proj.tasks`, `Celery(include=[…])`, `include` / `imports`). Otherwise the name is empty — it depends on how the worker imports the file, which the code does not show.

| Construct | Role | Record | Why |
|---|---|---|---|
| `@shared_task` / `@app.task` (with or without arguments) on a function | Binding | `eventbus_consumer`, `framework: celery`, `method: MESSAGE`, `endpoint` = the task's name (see above), `handler` = the function, at the decorator | A worker runs it when a message for that name arrives |
| A `beat_schedule` entry `{"task": "…", "schedule": …}` (`app.conf.beat_schedule`, `CELERY_BEAT_SCHEDULE`, `beat_schedule=`) | Binding | `timer`, `PERIODIC`, `endpoint` = the schedule, `handler` = the task the name leads to | A second trigger of the task: the schedule |
| `sender.add_periodic_task(schedule, task.s(…))` | Binding | as a beat entry; `handler` = the task referenced | A second trigger of the task |
| Schedule | — | `crontab(…)` → cron expression (`minute hour day-of-month month day-of-week`, unset `*`); seconds (`30.0`) or `timedelta(…)` → milliseconds; `solar(…)`, a variable → empty | |
| `task.delay(…)` / `task.apply_async(…)` (a task of this file, or imported — aliases followed) | Outbound | `eventbus_send`, `endpoint` = the task's name as its consumer writes it (empty when unknown) | Sends a message to the task |
| `app.send_task("name")` | Outbound | `eventbus_send`, `endpoint` = the literal (empty for a variable) | Sends by name |
| `Celery(…)`, `autodiscover_tasks`, `app.conf` settings | Wiring / None | — | Configuration; feeds the task names |
| `.delay()` on something not known to be a task | None | — | Not known to be Celery |
