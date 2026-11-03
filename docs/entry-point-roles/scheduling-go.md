# Scheduled jobs (Go) — entry-point roles

Parser: `src/breezeai_cog/parsers/go_scheduling/` (additive detector, Go files using robfig/cron or `time` tickers) · Rules: [entry-point contract](../architecture/entry-points.md) · Tests: `tests/unit/test_roles_scheduling_go.py`

Intervals are written in milliseconds, as for Quartz and Vert.x timers.

| Construct | Role | Record | Why |
|---|---|---|---|
| `c.AddFunc(spec, f)` / `c.AddJob(spec, job)` on `cron.New()` (robfig/cron) | Binding | `timer`, `method: PERIODIC`, `framework: cron`, `endpoint` = the spec (`0 2 * * *`, `@hourly`), `handler` = `f` / `job` | One handler, triggered by a schedule |
| `time.NewTicker(d)` / `time.Tick(d)` | Binding | `timer`, `PERIODIC`, `framework: time`, `endpoint` = `d` in ms, `handler` = the function the call is in | That function runs on every tick |
| `time.AfterFunc(d, f)` | Binding | `timer`, `TIMER`, `endpoint` = `d` in ms, `handler` = `f` | Runs once after the delay |
| An interval that is not a constant expression (`cfg.Every`) | Binding | `endpoint` empty | Never a guessed schedule |
| `time.After(d)` / `time.NewTimer(d)` / `time.Sleep(d)` | None | — | A timeout or pause in the code's own flow, not a schedule |
