"""Quartz role table: docs/entry-point-roles/quartz.md — one test per row, Java and C#."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

EP = {"route", "eventbus_consumer", "timer"}

JAVA = "src/main/java/app/Jobs.java"
JAVA_HEAD = """package app;
import org.quartz.*;
import static org.quartz.JobBuilder.newJob;
import static org.quartz.TriggerBuilder.newTrigger;
import static org.quartz.CronScheduleBuilder.cronSchedule;
import static org.quartz.SimpleScheduleBuilder.simpleSchedule;
public class Jobs {
"""
JAVA_JOB = """  static class HelloJob implements Job {
    public void execute(JobExecutionContext ctx) {}
  }
}
"""
CS = "Api/Program.cs"
CS_HEAD = "using Quartz;\npublic class Startup {\n"
CS_JOB = """}
public class HelloJob : IJob {
  public Task Execute(IJobExecutionContext context) { return Task.CompletedTask; }
}
"""


def _eps(stmts, path: str):
    return [s for s in stmts if is_entry_point(s) and s.path == path]


def _java(analyze_files, body: str):
    return _eps(analyze_files({JAVA: JAVA_HEAD + body + JAVA_JOB}), JAVA)


def _cs(analyze_files, body: str):
    return _eps(analyze_files({CS: CS_HEAD + body + CS_JOB}), CS)


def _line(src: str, text: str) -> int:
    return next(i for i, line in enumerate(src.splitlines(), 1) if text in line)


def test_schedule_job_with_job_and_trigger_is_binding(analyze_files) -> None:
    body = """  public void run(Scheduler sched) throws SchedulerException {
    JobDetail job = newJob(HelloJob.class).withIdentity("job1", "group1").build();
    Trigger t = newTrigger().withSchedule(cronSchedule("0 0 2 * * ?")).build();
    sched.scheduleJob(job, t);
  }
"""
    [r] = _java(analyze_files, body)
    src = JAVA_HEAD + body + JAVA_JOB
    assert (r.semanticType, r.framework, r.method, r.endpoint, r.handler, r.handlerLine,
            r.startLine) == ("timer", "quartz", "PERIODIC", "0 0 2 * * ?", "execute",
                             _line(src, "public void execute"), _line(src, "sched.scheduleJob"))

    cs_body = """  public async Task Run(IScheduler scheduler) {
    IJobDetail job = JobBuilder.Create<HelloJob>().WithIdentity("job1").Build();
    ITrigger trigger = TriggerBuilder.Create().WithCronSchedule("0 0/5 * * * ?").Build();
    await scheduler.ScheduleJob(job, trigger);
  }
"""
    [r] = _cs(analyze_files, cs_body)
    cs_src = CS_HEAD + cs_body + CS_JOB
    assert (r.method, r.endpoint, r.handler, r.handlerLine) == (
        "PERIODIC", "0 0/5 * * * ?", "Execute", _line(cs_src, "public Task Execute"))


def test_trigger_naming_its_job_is_binding(analyze_files) -> None:
    # Spring @Bean trigger: the job comes from a no-argument method returning it.
    body = """  public JobDetail reportJob() { return JobBuilder.newJob(HelloJob.class).storeDurably().build(); }
  public Trigger reportTrigger() {
    return TriggerBuilder.newTrigger().forJob(reportJob()).withSchedule(cronSchedule("0 0 2 * * ?")).build();
  }
  public Trigger byKey() {
    JobDetail job = newJob(HelloJob.class).withIdentity("job1", "group1").build();
    return newTrigger().forJob("job1", "group1").withSchedule(simpleSchedule().withIntervalInSeconds(40).repeatForever()).build();
  }
"""
    got = sorted((r.method, r.endpoint, r.handler) for r in _java(analyze_files, body))
    assert got == [("PERIODIC", "0 0 2 * * ?", "execute"), ("PERIODIC", "40000", "execute")]

    # Quartz.NET DI: the job is found through its key.
    cs_body = """  public void ConfigureServices(IServiceCollection services) {
    services.AddQuartz(q => {
      var key = new JobKey("hello");
      q.AddJob<HelloJob>(o => o.WithIdentity(key));
      q.AddTrigger(t => t.ForJob(key).WithCronSchedule("0 0 2 * * ?"));
      q.AddJob<HelloJob>(new JobKey("again"));
      q.AddTrigger(t => t.ForJob("again").WithSimpleSchedule(s => s.WithIntervalInMinutes(5).RepeatForever()));
    });
  }
"""
    got = sorted((r.method, r.endpoint, r.handler) for r in _cs(analyze_files, cs_body))
    assert got == [("PERIODIC", "0 0 2 * * ?", "Execute"), ("PERIODIC", "300000", "Execute")]


def test_schedule_job_of_a_type_is_binding(analyze_files) -> None:
    cs_body = """  public void ConfigureServices(IServiceCollection services) {
    services.AddQuartz(q => q.ScheduleJob<HelloJob>(t => t.StartNow()));
  }
"""
    [r] = _cs(analyze_files, cs_body)
    # No schedule: fires once — TIMER, nothing to put in endpoint.
    assert (r.method, r.endpoint, r.handler) == ("TIMER", None, "Execute")


def test_schedule_values(analyze_files) -> None:
    body = """  public void run(Scheduler sched) throws SchedulerException {
    JobDetail job = newJob(HelloJob.class).build();
    sched.scheduleJob(job, newTrigger().withSchedule(SimpleScheduleBuilder.repeatMinutelyForever(10)).build());
    sched.scheduleJob(job, newTrigger().withSchedule(cronSchedule(CRON)).build());
    sched.scheduleJob(job, newTrigger().withSchedule(DailyTimeIntervalScheduleBuilder.dailyTimeIntervalSchedule()).build());
    sched.scheduleJob(job, unknownTrigger);
    sched.scheduleJob(ctx.getDetail(), ctx.getTrigger());
  }
"""
    got = [(r.method, r.endpoint) for r in sorted(_java(analyze_files, body), key=lambda r: r.startLine)]
    assert got == [
        ("PERIODIC", "600000"),
        ("PERIODIC", None),   # a constant cron: not read → empty, never the name
        ("PERIODIC", None),   # a daily-time interval has no one value
        (None, None),         # a trigger that cannot be traced: neither kind nor schedule known
        (None, None),         # nor one from a call we cannot follow (it may well be a cron)
    ]


def test_schedule_job_with_trigger_only_has_no_record_of_its_own(analyze_files) -> None:
    body = """  public void run(Scheduler sched) throws SchedulerException {
    JobDetail job = newJob(HelloJob.class).withIdentity("job1").build();
    Trigger t = newTrigger().forJob("job1").withSchedule(cronSchedule("0 0 2 * * ?")).build();
    sched.scheduleJob(t);
  }
"""
    [r] = _java(analyze_files, body)  # the trigger naming its job is the binding
    assert r.text.startswith("Trigger t = ")


def test_cancellation_token_is_not_a_job_or_a_trigger(analyze_files) -> None:
    # Every Quartz.NET scheduler call takes a token: named or trailing positional, it is dropped.
    body = """  public async Task Run(IScheduler scheduler, CancellationToken ct) {
    IJobDetail job = JobBuilder.Create<HelloJob>().WithIdentity("job1").Build();
    ITrigger a = TriggerBuilder.Create().WithCronSchedule("0 0/5 * * * ?").Build();
    ITrigger b = TriggerBuilder.Create().WithCronSchedule("0 0 1 * * ?").Build();
    await scheduler.ScheduleJob(job, a, cancellationToken: ct);
    await scheduler.ScheduleJob(job, b, ct);
    ITrigger c = TriggerBuilder.Create().ForJob(job).WithCronSchedule("0 0 2 * * ?").Build();
    await scheduler.ScheduleJob(c, cancellationToken: ct);
  }
"""
    src = CS_HEAD + body + CS_JOB
    got = [(r.startLine, r.endpoint, r.handler) for r in _cs(analyze_files, body)]
    assert got == [
        (_line(src, "ScheduleJob(job, a"), "0 0/5 * * * ?", "Execute"),
        (_line(src, "ScheduleJob(job, b"), "0 0 1 * * ?", "Execute"),
        # the trigger naming its job is the binding; ScheduleJob(c, …) adds no record
        (_line(src, "ITrigger c ="), "0 0 2 * * ?", "Execute"),
    ]


def test_trigger_read_through_a_cast_is_traced(analyze_files) -> None:
    body = """  public async Task Run(IScheduler scheduler) {
    IJobDetail job = JobBuilder.Create<HelloJob>().Build();
    ICronTrigger a = (ICronTrigger) TriggerBuilder.Create().WithCronSchedule("0 0/5 * * * ?").Build();
    await scheduler.ScheduleJob(job, a);
    var b = TriggerBuilder.Create().WithCronSchedule("0 0 1 * * ?").Build() as ICronTrigger;
    await scheduler.ScheduleJob(job, b);
  }
"""
    got = [(r.method, r.endpoint, r.handler) for r in _cs(analyze_files, body)]
    assert got == [("PERIODIC", "0 0/5 * * * ?", "Execute"), ("PERIODIC", "0 0 1 * * ?", "Execute")]

    java_body = """  public void run(Scheduler sched) throws SchedulerException {
    JobDetail job = newJob(HelloJob.class).build();
    CronTrigger t = (CronTrigger) newTrigger().withSchedule(cronSchedule("0 0 2 * * ?")).build();
    sched.scheduleJob(job, t);
  }
"""
    [r] = _java(analyze_files, java_body)
    assert (r.method, r.endpoint, r.handler) == ("PERIODIC", "0 0 2 * * ?", "execute")


def test_timespan_interval_is_milliseconds(analyze_files) -> None:
    body = """  public async Task Run(IScheduler scheduler, TimeSpan every) {
    IJobDetail job = JobBuilder.Create<HelloJob>().Build();
    await scheduler.ScheduleJob(job, TriggerBuilder.Create().WithSimpleSchedule(x => x.WithInterval(TimeSpan.FromSeconds(40)).RepeatForever()).Build());
    await scheduler.ScheduleJob(job, TriggerBuilder.Create().WithSimpleSchedule(x => x.WithInterval(TimeSpan.FromHours(2))).Build());
    await scheduler.ScheduleJob(job, TriggerBuilder.Create().WithSimpleSchedule(x => x.WithInterval(TimeSpan.FromSeconds(2.5))).Build());
    await scheduler.ScheduleJob(job, TriggerBuilder.Create().WithSimpleSchedule(x => x.WithInterval(every)).Build());
    await scheduler.ScheduleJob(job, TriggerBuilder.Create().WithDailyTimeIntervalSchedule(x => x.WithInterval(10, IntervalUnit.Second)).Build());
  }
"""
    got = [(r.method, r.endpoint) for r in sorted(_cs(analyze_files, body), key=lambda r: r.startLine)]
    assert got == [
        ("PERIODIC", "40000"),
        ("PERIODIC", "7200000"),
        ("PERIODIC", None),   # not a whole number
        ("PERIODIC", None),   # a variable: not read
        ("PERIODIC", None),   # a count and a unit on a daily-time schedule: not one value
    ]


def test_job_class_is_none(analyze_files) -> None:
    assert _java(analyze_files, "") == []
    assert _cs(analyze_files, "") == []


def test_building_a_job_is_none(analyze_files) -> None:
    body = """  public JobDetail job() { return newJob(HelloJob.class).withIdentity("job1").build(); }
"""
    assert _java(analyze_files, body) == []
    cs_body = """  public void ConfigureServices(IServiceCollection services) {
    services.AddQuartz(q => q.AddJob<HelloJob>(o => o.WithIdentity("job1")));
  }
"""
    assert _cs(analyze_files, cs_body) == []


def test_trigger_without_a_job_is_none(analyze_files) -> None:
    body = """  public Trigger trigger() { return newTrigger().withSchedule(cronSchedule("0 0 2 * * ?")).build(); }
"""
    assert _java(analyze_files, body) == []


def test_job_class_in_another_file_leads_to_its_handler(analyze_files) -> None:
    # The job class is declared in its own file: the record leads there (handlerPath + line).
    body = '''  void Configure(IServiceCollectionQuartzConfigurator q) {
    q.ScheduleJob<ReportJob>(t => t.WithCronSchedule("0 0 1 * * ?"));
  }
}
'''
    st = analyze_files({CS: CS_HEAD + body, "Api/Jobs/ReportJob.cs": (
        "using Quartz;\npublic class ReportJob : IJob {\n"
        "  public Task Execute(IJobExecutionContext context) { return Task.CompletedTask; }\n}\n")})
    [r] = _eps(st, CS)
    assert (r.handler, r.handlerPath, r.handlerLine) == ("Execute", "Api/Jobs/ReportJob.cs", 3)
