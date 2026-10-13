"""Hangfire recurring jobs as timer bindings (parsers/csharp/hangfire.py)."""

from __future__ import annotations

SRC = """using System.Collections.Concurrent;
using Hangfire;

public class Jobs
{
    private readonly IRecurringJobManager _jobs;
    private readonly ConcurrentDictionary<string, int> _counts = new();

    public void Register()
    {
        _jobs.AddOrUpdate("report", () => SendReport(), Cron.Daily());
        RecurringJob.AddOrUpdate<Cleanup>("cleanup", c => c.Run(), Cron.Daily(2));
        RecurringJob.AddOrUpdate("tidy", () => Tidy(), Cron.Hourly);
        _counts.AddOrUpdate("k", 1, (key, v) => v + 1);
    }

    public void SendReport() { }
    public void Tidy() { }
}
"""


def _line(text: str) -> int:
    return next(i for i, line in enumerate(SRC.splitlines(), 1) if text in line)


def test_recurring_jobs_are_timers(analyze_files) -> None:
    timers = [s for s in analyze_files({"Jobs.cs": SRC}) if s.semanticType == "timer"]
    got = sorted((s.startLine, s.endpoint or "", s.handler, s.handlerLine) for s in timers)
    assert got == [
        (_line('"report"'), "0 0 * * *", "SendReport", _line("public void SendReport")),
        # Cron.Daily(2) is not resolved (only the no-argument helpers are): empty. Run is
        # declared in another file: name only.
        (_line('"cleanup"'), "", "Run", None),
        (_line('"tidy"'), "0 * * * *", "Tidy", _line("public void Tidy")),
    ]
    assert {(s.framework, s.method) for s in timers} == {("hangfire", "PERIODIC")}
