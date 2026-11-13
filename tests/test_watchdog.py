"""The three failures the watchdog exists for - each must be DETECTED and RECOVERED (or, where
recovery is impossible, reported red with what to do) - plus the checks around them."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import watchdog as W

PERTH = ZoneInfo("Australia/Perth")
CFG = W.load_config()


def perth(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=PERTH).astimezone(timezone.utc)


class Clock:
    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)


class FakeGitHub:
    """Holds runs / artifacts / files per repo. `on_dispatch` lets a test say what a re-run does."""

    def __init__(self, clock):
        self.clock = clock
        self.runs_by = {}           # (repo, workflow) -> [run]
        self.artifacts = set()      # (repo, name)
        self.files = {}             # (repo, path) -> text
        self.vars = {}              # (repo, name) -> value
        self.annotations = {}       # run id -> [messages]
        self.dispatched = []
        self.on_dispatch = None
        self.down = False           # Actions outage: every call raises
        self.billing = False        # billing block: dispatch refused

    def _gate(self):
        if self.down:
            raise W.ActionsUnavailable("GitHub 503")

    def add_run(self, repo, wf, when, conclusion, run_id=None):
        iso = when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        self.runs_by.setdefault((repo, wf), []).insert(
            0, {"id": run_id or len(self.dispatched) + 1000 + len(self.runs_by), "conclusion": conclusion,
                "created_at": iso, "updated_at": iso})

    def runs(self, repo, wf, per_page=30):
        self._gate()
        return list(self.runs_by.get((repo, wf), []))

    def artifact_exists(self, repo, name):
        self._gate()
        return (repo, name) in self.artifacts

    def variable(self, repo, name):
        self._gate()
        return self.vars.get((repo, name))

    def file_text(self, repo, path, ref):
        self._gate()
        return self.files.get((repo, path))

    def job_annotations(self, repo, run_id):
        self._gate()
        return self.annotations.get(run_id, [])

    def dispatch(self, repo, wf, ref):
        self._gate()
        if self.billing:
            raise W.BillingBlocked("recent account payments have failed")
        self.dispatched.append((repo, wf))
        if self.on_dispatch:
            self.on_dispatch(repo, wf)


class FakeStatus:
    def __init__(self, status="operational"):
        self.s = status

    def actions_status(self):
        return self.s


class FakeBrevo:
    def __init__(self, events):
        self.list = events

    def events(self, start, end):
        return list(self.list)


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send(self, to, sender, subject, html_body, text_body):
        self.sent.append((to, subject, text_body))
        return "fake"


class FakeRunner:
    def __init__(self, code=0):
        self.code, self.ran = code, []

    def run(self, command):
        self.ran.append(command)
        return self.code


def job(job_id):
    return next(j for j in CFG["automations"] if j["id"] == job_id)


def clients(clock, gh, brevo=None, runner=None, status="operational"):
    return W.Clients(github=gh, status=FakeStatus(status), brevo=brevo, mailer=FakeMailer(),
                     runner=runner or FakeRunner(), now=clock.now, sleep=clock.sleep)


def delivered(subject, when, *rcpts):
    iso = when.astimezone(timezone.utc).isoformat()
    return [{"email": r, "subject": subject, "date": iso, "event": e}
            for r in rcpts for e in ("requests", "delivered")]


# ── 1. missed cron: the run never happened -> detected, re-triggered, recovered ──────────────

def test_missed_cron_is_detected_retriggered_and_recovered():
    clock = Clock(perth(2026, 11, 13, 7, 30))
    gh = FakeGitHub(clock)
    gh.add_run("nmg-ohs-dashboard", "dashboard.yml", perth(2026, 11, 13, 1, 0), "success")  # 6.5 h ago

    def rerun(repo, wf):                  # the dispatched build succeeds a minute later
        gh.add_run(repo, wf, clock.now() + timedelta(minutes=1), "success")
    gh.on_dispatch = rerun

    res = W.watch(job("ohs_dashboard"), clients(clock, gh), CFG)

    assert gh.dispatched == [("nmg-ohs-dashboard", "dashboard.yml")]
    assert "recovered" in res.action
    assert res.status == W.AMBER          # recovered, but it HAD missed - not silently green


def test_missed_daily_email_is_retriggered_and_its_delivery_checked():
    clock = Clock(perth(2026, 11, 13, 7, 30))
    gh = FakeGitHub(clock)
    subj = "NMG Crown Prince Operation Evacuation List 13/11/2026"
    brevo = FakeBrevo([])

    def rerun(repo, wf):
        gh.artifacts.add(("nmg-evac-list", "evac-sent-2026-11-13"))
        brevo.list += delivered(subj, clock.now(), "a@x.au", "b@x.au")
    gh.on_dispatch = rerun

    res = W.watch(job("evac_email"), clients(clock, gh, brevo), CFG)

    assert gh.dispatched == [("nmg-evac-list", "evac_send.yml")]
    assert "recovered" in res.action and "delivered 2/2" in res.detail
    assert res.status == W.AMBER


def test_missed_email_past_its_cutoff_is_red_with_the_manual_step_not_retriggered():
    clock = Clock(perth(2026, 11, 13, 9, 30))
    gh = FakeGitHub(clock)
    res = W.watch(job("evac_email"), clients(clock, gh, FakeBrevo([])), CFG)
    assert gh.dispatched == []
    assert res.status == W.RED and "force=true" in res.action


def test_retrigger_that_never_lands_is_red():
    clock = Clock(perth(2026, 11, 13, 7, 30))
    gh = FakeGitHub(clock)                # dispatch accepted, but nothing ever runs
    res = W.watch(job("mobs_email"), clients(clock, gh, FakeBrevo([])), CFG, wait_minutes=5)
    assert gh.dispatched and res.status == W.RED and "still not done" in res.action


# ── 2. Actions outage / billing block -> detected; local fallback when allowed ───────────────

def test_actions_outage_is_detected_and_reported_red():
    clock = Clock(perth(2026, 11, 13, 7, 30))
    gh = FakeGitHub(clock)
    gh.down = True
    res = W.watch(job("ert_dashboard"), clients(clock, gh), CFG)
    assert res.status == W.RED and "unavailable" in res.detail


def test_billing_block_on_dispatch_falls_back_to_a_local_run():
    clock = Clock(perth(2026, 11, 13, 7, 30))
    gh = FakeGitHub(clock)
    gh.billing = True
    gh.add_run("nmg-ert-dashboard", "dashboard.yml", perth(2026, 11, 12, 17, 0), "success")  # 14.5 h old
    runner = FakeRunner(code=0)
    local_job = dict(job("ert_dashboard"), local_command={"run": "python generate_dashboard.py", "cwd": "."})

    res = W.watch(local_job, clients(clock, gh, runner=runner), CFG, allow_local=True)

    assert runner.ran == [local_job["local_command"]]
    assert "ran locally (exit 0)" in res.action and res.status == W.AMBER


def test_billing_block_without_local_fallback_is_red_with_manual_step():
    clock = Clock(perth(2026, 11, 13, 7, 30))
    gh = FakeGitHub(clock)
    gh.billing = True
    gh.add_run("nmg-ert-dashboard", "dashboard.yml", perth(2026, 11, 12, 17, 0), "success")
    runner = FakeRunner()
    res = W.watch(job("ert_dashboard"), clients(clock, gh, runner=runner), CFG, allow_local=True)
    assert runner.ran == []                       # no local_command configured -> never improvise one
    assert res.status == W.RED and "billing blocked" in res.action and "gh workflow run" in res.action


def test_billing_refused_runs_today_are_reported_even_when_a_later_run_succeeded():
    clock = Clock(perth(2026, 11, 13, 7, 30))
    gh = FakeGitHub(clock)
    gh.add_run("nmg-ert-dashboard", "daily_roster.yml", perth(2026, 11, 13, 4, 57), "failure", run_id=77)
    gh.annotations[77] = ["The job was not started because recent account payments have failed."]
    gh.add_run("nmg-ert-dashboard", "daily_roster.yml", perth(2026, 11, 13, 6, 40), "success")
    subj = "Crown Prince Operation ERT Roster — Fri 13 Nov 2026"
    res = W.watch(job("ert_roster_email"), clients(clock, gh, FakeBrevo(delivered(subj, perth(2026, 11, 13, 6, 41), "a@x.au"))), CFG)
    assert any("billing" in n for n in res.notes) and res.status == W.AMBER


def test_startup_failure_and_skips_are_noted():
    clock = Clock(perth(2026, 11, 13, 12, 0))
    gh = FakeGitHub(clock)
    gh.add_run("nmg-ohs-dashboard", "dashboard.yml", perth(2026, 11, 13, 4, 45), "startup_failure")
    gh.add_run("nmg-ohs-dashboard", "dashboard.yml", perth(2026, 11, 13, 6, 0), "skipped")
    gh.add_run("nmg-ohs-dashboard", "dashboard.yml", perth(2026, 11, 13, 11, 30), "success")
    res = W.watch(job("ohs_dashboard"), clients(clock, gh), CFG)
    assert any("startup_failure" in n for n in res.notes) and any("skipped" in n for n in res.notes)
    assert res.status == W.AMBER


# ── 3. Brevo bounce: "sent" is not enough -> bounce detected, red, address in digest only ─────

def test_brevo_bounce_is_red_and_named_in_the_digest_but_not_the_logs(capsys):
    clock = Clock(perth(2026, 11, 13, 7, 30))
    gh = FakeGitHub(clock)
    gh.artifacts.add(("nmg-evac-list", "evac-sent-2026-11-13"))
    subj = "NMG Crown Prince Operation Evacuation List 13/11/2026"
    sent = perth(2026, 11, 13, 5, 0)
    events = delivered(subj, sent, "ok@x.au") + [
        {"email": "MESO@client.example", "subject": subj, "date": sent.isoformat(), "event": "requests"},
        {"email": "MESO@client.example", "subject": subj, "date": sent.isoformat(), "event": "hardBounces",
         "reason": "550 rejected by policy"}]
    c = clients(clock, gh, FakeBrevo(events))

    results, subject, text = W.run({**CFG, "automations": [job("evac_email")]}, c, env={"DIGEST_TO": "me@x.au"})

    res = results[0]
    assert res.status == W.RED and "bounced 1" in res.detail and "delivered 1/2" in res.detail
    assert "meso@client.example: hardBounces - 550 rejected by policy" in text      # in the email
    assert "client.example" not in capsys.readouterr().out                                 # not in logs
    assert c.mailer.sent and "1 red" in c.mailer.sent[0][1]


def test_accepted_but_not_yet_delivered_is_amber():
    clock = Clock(perth(2026, 11, 13, 7, 30))
    gh = FakeGitHub(clock)
    gh.artifacts.add(("nmg-mobs", "mobs-sent-2026-11-13"))
    ev = [{"email": "a@x.au", "subject": "Mobilisation compliance - 3 booked", "event": "deferred",
           "date": perth(2026, 11, 13, 5, 1).isoformat()}]
    res = W.watch(job("mobs_email"), clients(clock, gh, FakeBrevo(ev)), CFG)
    assert res.status == W.AMBER and "pending 1" in res.detail


def test_without_a_brevo_key_delivery_is_never_claimed():
    clock = Clock(perth(2026, 11, 13, 7, 30))
    gh = FakeGitHub(clock)
    gh.artifacts.add(("nmg-evac-list", "evac-sent-2026-11-13"))
    res = W.watch(job("evac_email"), clients(clock, gh, brevo=None), CFG)
    assert res.status == W.AMBER and "not confirmed" in res.detail


# ── the rest of the checks ────────────────────────────────────────────────────────────────

def test_everything_fine_is_green_and_nothing_is_triggered():
    clock = Clock(perth(2026, 11, 13, 7, 30))
    gh = FakeGitHub(clock)
    gh.add_run("nmg-ert-dashboard", "dashboard.yml", perth(2026, 11, 13, 6, 40), "success")
    res = W.watch(job("ert_dashboard"), clients(clock, gh), CFG)
    assert res.status == W.GREEN and gh.dispatched == []


def test_held_roster_email_is_amber_not_a_failure():
    clock = Clock(perth(2026, 11, 13, 7, 30))
    gh = FakeGitHub(clock)
    gh.vars[("nmg-ert-dashboard", "ROSTER_EMAIL_HOLD")] = "true"
    res = W.watch(job("ert_roster_email"), clients(clock, gh), CFG)
    assert res.status == W.AMBER and "held" in res.detail and gh.dispatched == []


def test_eom_not_due_before_7_on_the_1st_and_checked_after():
    gh = FakeGitHub(None)
    gh.files[("nmg-eom", "data/history.yaml")] = "months:\n  2026-10:\n    cells: {}\n"
    early = Clock(perth(2026, 11, 1, 6, 30))
    gh.clock = early
    assert W.watch(job("eom_report"), clients(early, gh), CFG).status == W.GREEN      # not due yet
    later = Clock(perth(2026, 11, 13, 7, 30))     # mid-month: the 1st's delivery still counts
    res = W.watch(job("eom_report"), clients(later, gh, FakeBrevo(
        delivered("NMG CPO EOM HSEC statistics - October 2026", perth(2026, 11, 1, 7, 0), "a@x.au"))), CFG)
    assert res.status == W.GREEN and "2026-10 sent" in res.detail


def test_eom_month_missing_after_the_1st_is_retriggered():
    clock = Clock(perth(2026, 12, 1, 7, 30))
    gh = FakeGitHub(clock)
    gh.files[("nmg-eom", "data/history.yaml")] = "months:\n  2026-10: {}\n"
    W.watch(job("eom_report"), clients(clock, gh), CFG, wait_minutes=1)
    assert gh.dispatched == [("nmg-eom", "eom_send.yml")]


def test_dry_run_never_dispatches_or_emails():
    clock = Clock(perth(2026, 11, 13, 7, 30))
    gh = FakeGitHub(clock)
    c = clients(clock, gh)
    results, _, _ = W.run(CFG, c, dry_run=True, env={})
    assert gh.dispatched == [] and c.mailer.sent == []
    assert all(r.status in (W.RED, W.AMBER, W.GREEN) for r in results)


def test_config_lists_every_automation_with_a_schedule_and_manual_step():
    ids = {j["id"] for j in CFG["automations"]}
    assert ids == {"ert_dashboard", "ohs_dashboard", "ert_roster_email", "evac_email", "mobs_email", "eom_report"}
    for j in CFG["automations"]:
        assert j["schedule"] and j["manual"].startswith("gh workflow run") and j["check"]


def test_a_refused_dispatch_is_never_reported_as_retriggered():
    clock = Clock(perth(2026, 11, 13, 7, 30))
    gh = FakeGitHub(clock)

    def refuse(repo, wf, ref):
        raise RuntimeError("dispatch refused (404): check WATCHDOG_TOKEN access to nmg-mobs")
    gh.dispatch = refuse
    res = W.watch(job("mobs_email"), clients(clock, gh, FakeBrevo([])), CFG)
    assert res.status == W.RED and "refused" in res.action and "re-triggered (" not in res.action


def test_missing_token_stops_with_a_clear_message(monkeypatch, capsys):
    monkeypatch.delenv("WATCHDOG_TOKEN", raising=False)
    assert W.main(["--dry-run"]) == 1
    assert "WATCHDOG_TOKEN is not set" in capsys.readouterr().out


def test_a_repo_the_token_cannot_see_is_named_and_never_retriggered():
    clock = Clock(perth(2026, 11, 13, 8, 0))
    gh = FakeGitHub(clock)

    def hidden(repo, wf, per_page=30):
        raise W.TokenCannotSee(f"WATCHDOG_TOKEN cannot see {repo} ({wf}) - add the repo to the token")
    gh.runs = hidden
    res = W.watch(job("ert_dashboard"), clients(clock, gh), CFG)
    assert res.status == W.RED and "cannot see nmg-ert-dashboard" in res.detail and gh.dispatched == []
