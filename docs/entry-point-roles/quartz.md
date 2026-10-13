# Quartz (Java) and Quartz.NET (C#) — entry-point roles

Parser: `src/breezeai_cog/parsers/quartz_common.py` (shared rules), `java/quartz.py`, `csharp/quartz.py` (additive detectors) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_quartz.py`

A job class says what runs, a trigger says when. The binding is the first construct that names both.

| Construct | Role | Record | Why |
|---|---|---|---|
| `scheduler.scheduleJob(job, trigger)` / `ScheduleJob(job, trigger)` | Binding | `timer`, `framework: quartz`, schedule as `endpoint`, `handler` = `execute` / `Execute` of the job's class (its line when declared in the file) | Names one job and its schedule |
| A trigger built with `forJob(…)` — `newTrigger().forJob(reportJob())…build()` (also a Spring `@Bean`), `q.AddTrigger(t => t.ForJob(key)…)` | Binding | as above; the job found through the key (`withIdentity` ↔ `forJob`), a job variable or a no-argument method returning the job | Names one job and its schedule |
| `q.ScheduleJob<ReportJob>(t => …)` (Quartz.NET DI) | Binding | as above | Names one job and its schedule |
| Schedule | — | cron (`withCronSchedule` / `cronSchedule`) → `PERIODIC`, the expression; fixed interval (`withIntervalInSeconds(40)`, `repeatMinutelyForever(5)`) → `PERIODIC`, milliseconds (`40000`); none → `TIMER` (fires once), empty; daily-time / calendar interval, a non-literal → `PERIODIC`, empty | |
| `scheduler.scheduleJob(trigger)` (one argument) | None | — | Its trigger names the job with `forJob`: that is the binding |
| Job class `implements Job` / `: IJob` | None | — (it is the handler) | No schedule of its own |
| Building a job: `newJob(R.class)…build()`, `JobBuilder.Create<R>()`, `q.AddJob<R>(…)` | None | — | Names a job, no schedule |
| Building a trigger without `forJob` | None | — | A schedule, no job (until `scheduleJob(job, trigger)`) |
