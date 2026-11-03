"""Hangfire recurring jobs as timer bindings (parsers/csharp/hangfire.py)."""

from __future__ import annotations

import pytest

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
        # Cron.Daily(2): Hangfire's formula. Cleanup is not in the repo: name only.
        (_line('"cleanup"'), "0 2 * * *", "Run", None),
        (_line('"tidy"'), "0 * * * *", "Tidy", _line("public void Tidy")),
    ]
    assert {(s.framework, s.method) for s in timers} == {("hangfire", "PERIODIC")}


@pytest.mark.parametrize(("helper", "expected"), [
    ("Cron.Daily(2, 30)", "30 2 * * *"),
    ("Cron.Daily(minute: 5)", "5 0 * * *"),
    ("Cron.Weekly(DayOfWeek.Friday, hour: 3)", "0 3 * * 5"),
    ("Cron.Weekly()", "0 0 * * 1"),
    ("Cron.Monthly(15)", "0 0 15 * *"),
    ("Cron.Yearly(6, 1)", "0 0 1 6 *"),
    ("Cron.MinuteInterval(5)", "*/5 * * * *"),
    # Not literals, or not fitting the helper: empty, never a guess.
    ("Cron.Daily(hour)", None),
    ("Cron.Daily(1, 2, 3)", None),
    ("Cron.MinuteInterval()", None),
])
def test_cron_helper_arguments(analyze_files, helper: str, expected: str | None) -> None:
    src = (
        "using Hangfire;\npublic class J {\n  public void Register(int hour) {\n"
        f'    RecurringJob.AddOrUpdate("j", () => Run(), {helper});\n'
        "  }\n  public void Run() {}\n}\n"
    )
    [timer] = [s for s in analyze_files({"Jobs.cs": src}) if s.semanticType == "timer"]
    assert timer.endpoint == expected


_STARTUP = """using Hangfire;
namespace N {
  public class Startup {
    public void Run() { }
    public void Send() { }
    public void Configure(Job job) {
      RecurringJob.AddOrUpdate<Cleanup>("j", JOB, "0 2 * * *");
    }
  }
}
"""
_OTHER = """namespace N {
  public class Cleanup { public void Run() { } }
  public class Reports { public static void Send() { } }
  public class Job { public void Run() { } }
}
"""


@pytest.mark.parametrize(("job", "where"), [
    # a call on the lambda's parameter: the type argument's method, not Startup.Run
    ("c => c.Run()", ("Other.cs", 2)),
    ("() => Reports.Send()", ("Other.cs", 3)),  # a static call on a type
    ("() => Send()", (None, 5)),  # a bare call: the registering class's own method
    ("() => job.Run()", (None, None)),  # a variable: its class is not known
])
def test_handler_is_located_in_the_job_class(analyze_files, job: str, where) -> None:
    st = analyze_files({"Startup.cs": _STARTUP.replace("JOB", job), "Other.cs": _OTHER})
    [t] = [s for s in st if s.semanticType == "timer"]
    assert (t.handlerPath, t.handlerLine) == where
