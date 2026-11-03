"""Scheduled jobs (Go) role table (docs/entry-point-roles/scheduling-go.md): one test per row."""

from __future__ import annotations

MOD = "module example.com/app\n\ngo 1.22\n"


def _records(analyze_files, imp: str, body: str):
    st = analyze_files({"go.mod": MOD, "main.go": f"package main\n\nimport {imp}\n\n" + body})
    return {(s.semanticType, s.method, s.framework, s.endpoint, s.handler) for s in st if s.semanticType == "timer"}


def test_cron_add_func(analyze_files) -> None:
    assert _records(analyze_files, '"github.com/robfig/cron/v3"',
                    'func main() {\n\tc := cron.New()\n\tc.AddFunc("0 2 * * *", nightly)\n\tc.AddJob("@hourly", job)\n}\n') \
        == {("timer", "PERIODIC", "cron", "0 2 * * *", "nightly"), ("timer", "PERIODIC", "cron", "@hourly", "job")}


def test_ticker(analyze_files) -> None:
    assert _records(analyze_files, '"time"', 'func poll() {\n\tt := time.NewTicker(30 * time.Second)\n\t_ = t\n'
                                            '\tfor range time.Tick(time.Minute) {}\n}\n') \
        == {("timer", "PERIODIC", "time", "30000", "poll"), ("timer", "PERIODIC", "time", "60000", "poll")}


def test_after_func(analyze_files) -> None:
    assert _records(analyze_files, '"time"', 'func main() {\n\ttime.AfterFunc(2*time.Hour+30*time.Minute, cleanup)\n}\n') \
        == {("timer", "TIMER", "time", "9000000", "cleanup")}


def test_interval_from_config_has_no_endpoint(analyze_files) -> None:
    assert _records(analyze_files, '"time"', 'func poll(cfg Config) {\n\ttime.NewTicker(cfg.Every)\n}\n') \
        == {("timer", "PERIODIC", "time", None, "poll")}


def test_timeouts_are_not_schedules(analyze_files) -> None:
    assert _records(analyze_files, '"time"', 'func main() {\n\t<-time.After(time.Second)\n\ttime.NewTimer(time.Second)\n'
                                            '\ttime.Sleep(time.Second)\n}\n') == set()
