"""NMG Watchdog: checks every scheduled automation once a day and emails one digest.

For each automation in watchdog/config.yaml it asks GitHub whether the job did its job today
(a fresh successful build, a sent-marker artifact, a successful send run, a frozen month),
spots runs that were billing-blocked, failed to start or were skipped, and asks Brevo whether
each email was DELIVERED to each recipient - not just accepted. An overdue job is re-triggered
with workflow_dispatch (each job's own guard stops a double send) and re-checked; if Actions
cannot start it, a configured local command runs instead (only with --allow-local, i.e. on the
PC). The digest gives every automation a green / amber / red status and says what was done.

Run:  python watchdog.py                 # full run: check, recover, email the digest
      python watchdog.py --dry-run       # check only: no dispatch, no local runs, no email
Env:  WATCHDOG_TOKEN (GitHub PAT: Actions read+write, Contents read, Variables read on the
      watched repos), BREVO_API_KEY (events + digest send), DIGEST_TO, optional DIGEST_FROM,
      or BREVO_USERNAME / BREVO_PASSWORD to send the digest by SMTP when there is no API key.
Logs print statuses and counts only - never addresses (the repo is public).
"""
import argparse
import base64
import html
import json
import os
import re
import smtplib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "watchdog" / "config.yaml"
GITHUB_API = "https://api.github.com"
STATUS_API = "https://www.githubstatus.com/api/v2/components.json"
BREVO_API = "https://api.brevo.com/v3"
DEFAULT_FROM = "neil.noble@emergencyservicesaustralia.com.au"

GREEN, AMBER, RED = "green", "amber", "red"
RANK = {GREEN: 0, AMBER: 1, RED: 2}
BILLING_WORDS = re.compile(r"payments have failed|spending limit|billing", re.I)
DELIVERED = {"delivered"}
FAILED = {"hardBounces", "softBounces", "hardBounce", "softBounce", "blocked", "invalid",
          "error", "spam"}


# ── errors ────────────────────────────────────────────────────────────────────────────────

class ActionsUnavailable(Exception):
    """GitHub could not be reached or answered 5xx: Actions cannot start anything right now."""


class BillingBlocked(Exception):
    """GitHub refused to start a job because of a billing / spending-limit problem."""


# ── clients (real ones; tests pass fakes with the same methods) ────────────────────────────

def _http(method, url, headers=None, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else None)


class GitHub:
    def __init__(self, token, owner):
        self.owner = owner
        self.headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                        "User-Agent": "nmg-watchdog"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def _call(self, method, path, body=None):
        try:
            return _http(method, GITHUB_API + path, self.headers, body)[1]
        except urllib.error.HTTPError as err:
            text = err.read().decode("utf-8", "replace")
            if err.code >= 500:
                raise ActionsUnavailable(f"GitHub {err.code} on {path}") from err
            if err.code in (402, 403, 422) and BILLING_WORDS.search(text):
                raise BillingBlocked(text[:200]) from err
            if err.code == 404:
                return None
            raise RuntimeError(f"GitHub {err.code} on {path}: {text[:200]}") from err
        except (urllib.error.URLError, TimeoutError) as err:
            raise ActionsUnavailable(f"GitHub unreachable: {err}") from err

    def runs(self, repo, workflow, per_page=30):
        # Unfiltered on purpose: GitHub's ?status= filter has returned stale indexes (ERT 2026-09-28).
        got = self._call("GET", f"/repos/{self.owner}/{repo}/actions/workflows/{workflow}/runs?per_page={per_page}")
        return (got or {}).get("workflow_runs", [])

    def artifact_exists(self, repo, name):
        got = self._call("GET", f"/repos/{self.owner}/{repo}/actions/artifacts?name={urllib.parse.quote(name)}&per_page=5")
        return any(a.get("name") == name and not a.get("expired") for a in (got or {}).get("artifacts", []))

    def variable(self, repo, name):
        got = self._call("GET", f"/repos/{self.owner}/{repo}/actions/variables/{name}")
        return None if got is None else str(got.get("value", ""))

    def file_text(self, repo, path, ref):
        got = self._call("GET", f"/repos/{self.owner}/{repo}/contents/{path}?ref={ref}")
        if not got:
            return None
        if got.get("content"):
            return base64.b64decode(got["content"]).decode("utf-8")
        with urllib.request.urlopen(urllib.request.Request(got["download_url"], headers=self.headers), timeout=30) as r:
            return r.read().decode("utf-8")

    def job_annotations(self, repo, run_id):
        """Messages GitHub attached to a run's jobs (where a billing refusal is written)."""
        jobs = (self._call("GET", f"/repos/{self.owner}/{repo}/actions/runs/{run_id}/jobs") or {}).get("jobs", [])
        out = []
        for job in jobs:
            notes = self._call("GET", f"/repos/{self.owner}/{repo}/check-runs/{job['id']}/annotations") or []
            out += [n.get("message", "") for n in notes]
        return out

    def dispatch(self, repo, workflow, ref):
        try:
            _http("POST", f"{GITHUB_API}/repos/{self.owner}/{repo}/actions/workflows/{workflow}/dispatches",
                  self.headers, {"ref": ref})
        except urllib.error.HTTPError as err:
            text = err.read().decode("utf-8", "replace")
            if err.code >= 500:
                raise ActionsUnavailable(f"GitHub {err.code} on dispatch") from err
            if BILLING_WORDS.search(text):
                raise BillingBlocked(text[:200]) from err
            # 404/403 here means the token cannot see or run the workflow - never "re-triggered".
            raise RuntimeError(f"dispatch refused ({err.code}): check WATCHDOG_TOKEN access to {repo}") from err
        except (urllib.error.URLError, TimeoutError) as err:
            raise ActionsUnavailable(f"GitHub unreachable: {err}") from err


class GitHubStatus:
    def actions_status(self):
        """'operational', 'degraded_performance', 'partial_outage', 'major_outage', or 'unknown'."""
        try:
            comps = _http("GET", STATUS_API, {"User-Agent": "nmg-watchdog"})[1].get("components", [])
        except Exception:
            return "unknown"
        for comp in comps:
            if comp.get("name") == "Actions":
                return comp.get("status", "unknown")
        return "unknown"


class Brevo:
    def __init__(self, api_key):
        self.api_key = api_key

    def events(self, start, end):
        """All transactional events between two dates (inclusive), newest first, every page."""
        out, offset = [], 0
        while True:
            q = urllib.parse.urlencode({"startDate": start.isoformat(), "endDate": end.isoformat(),
                                        "limit": 2500, "offset": offset, "sort": "desc"})
            got = _http("GET", f"{BREVO_API}/smtp/statistics/events?{q}",
                        {"api-key": self.api_key, "Accept": "application/json"})[1] or {}
            page = got.get("events", [])
            out += page
            if len(page) < 2500:
                return out
            offset += 2500


class Mailer:
    def __init__(self, env):
        self.env = env

    def send(self, to, sender, subject, html_body, text_body):
        key = self.env.get("BREVO_API_KEY")
        if key:
            _http("POST", f"{BREVO_API}/smtp/email",
                  {"api-key": key, "Accept": "application/json"},
                  {"sender": {"email": sender, "name": "NMG Watchdog"}, "to": [{"email": to}],
                   "subject": subject, "htmlContent": html_body, "textContent": text_body})
            return "brevo-api"
        user, password = self.env.get("BREVO_USERNAME"), self.env.get("BREVO_PASSWORD")
        if not (user and password):
            raise RuntimeError("no BREVO_API_KEY and no BREVO_USERNAME/BREVO_PASSWORD: cannot send the digest")
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, sender, to
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
        with smtplib.SMTP("smtp-relay.brevo.com", 587, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
        return "brevo-smtp"


class LocalRunner:
    def run(self, command):
        proc = subprocess.run(command["run"], shell=True, cwd=command.get("cwd"), capture_output=True,
                              text=True, timeout=command.get("timeout", 1800))
        return proc.returncode


@dataclass
class Clients:
    github: object
    status: object
    brevo: object            # None when there is no API key
    mailer: object
    runner: object
    now: object              # () -> aware datetime
    sleep: object = time.sleep


# ── results ───────────────────────────────────────────────────────────────────────────────

@dataclass
class Result:
    id: str
    name: str
    status: str = GREEN
    detail: str = ""
    action: str = ""
    notes: list = field(default_factory=list)       # extra lines (digest only)
    private: list = field(default_factory=list)     # lines with addresses: digest only, never logs

    def worsen(self, status, note=None):
        if RANK[status] > RANK[self.status]:
            self.status = status
        if note:
            self.notes.append(note)


def parse_time(text):
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def at(day, hhmm, tz):
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=tz)


def last_month(day):
    first = day.replace(day=1)
    prev = first - timedelta(days=1)
    return f"{prev.year:04d}-{prev.month:02d}"


# ── one check per kind: returns (done: bool | None, detail) - None = not due yet ───────────

def check_done(job, gh, now, tz):
    today = now.astimezone(tz).date()
    kind = job["check"]
    if kind == "freshness":
        runs = gh.runs(job["repo"], job["workflow"])
        ok = [r for r in runs if r.get("conclusion") == "success"]
        if not ok:
            return False, "no successful run on record"
        last = max(parse_time(r.get("updated_at") or r.get("created_at")) for r in ok)
        age = now - last
        hours = age.total_seconds() / 3600
        fresh = hours <= job["max_age_hours"]
        return fresh, f"last good build {hours:.1f} h ago (limit {job['max_age_hours']} h)"

    due = at(today, job.get("due", "00:00"), tz) + timedelta(minutes=job.get("grace_minutes", 0))
    if kind == "monthly_file":
        # Due on the 1st at `due` (+grace); any later day of the month, last month must be there.
        if today.day == 1 and now < due:
            return None, f"{last_month(today)} due {due:%H:%M} today"
        month = last_month(today)
        text = gh.file_text(job["repo"], job["path"], job.get("ref", "master"))
        sent = bool(text) and month in ((yaml.safe_load(text) or {}).get("months") or {})
        return sent, (f"{month} sent" if sent else f"{month} not sent")

    if now < due:
        return None, f"not due until {due:%H:%M}"
    if kind == "daily_marker":
        name = f"{job['marker']}-{today.isoformat()}"
        sent = gh.artifact_exists(job["repo"], name)
        return sent, ("sent today" if sent else "no sent marker for today")
    if kind == "daily_success":
        runs = gh.runs(job["repo"], job["workflow"])
        good = [r for r in runs if r.get("conclusion") == "success"
                and parse_time(r.get("created_at")).astimezone(tz).date() == today]
        return bool(good), ("sent today" if good else "no successful send run today")
    raise ValueError(f"unknown check kind {kind!r} for {job['id']}")


def run_problems(job, gh, now, tz):
    """Notes about today's runs that did not do their job: billing, start failures, skips."""
    today = now.astimezone(tz).date()
    runs = [r for r in gh.runs(job["repo"], job["workflow"])
            if parse_time(r.get("created_at")).astimezone(tz).date() == today]
    billing, startup, skipped, failed = 0, 0, 0, 0
    for r in runs:
        c = r.get("conclusion")
        if c == "startup_failure":
            startup += 1
        elif c in ("skipped", "cancelled"):
            skipped += 1
        elif c == "failure":
            failed += 1
            if any(BILLING_WORDS.search(m) for m in gh.job_annotations(job["repo"], r["id"])):
                billing += 1
    return {"billing": billing, "startup_failure": startup, "skipped": skipped, "failed": failed - billing,
            "runs": len(runs)}


def delivery(job, brevo, now, tz):
    """(status, summary, private lines) from Brevo's events for today's email."""
    if not job.get("email_subject"):
        return GREEN, "", []
    if brevo is None:
        return AMBER, "accepted only - delivery not confirmed (no Brevo API key)", []
    today = now.astimezone(tz).date()
    first = today.replace(day=1) if job["check"] == "monthly_file" else today   # monthly mail goes on the 1st
    start = datetime(first.year, first.month, first.day, tzinfo=tz)
    events = [e for e in brevo.events(first - timedelta(days=1), today)
              if (e.get("subject") or "").startswith(job["email_subject"])
              and parse_time(e["date"]) >= start]
    if not events:
        return AMBER, "no Brevo events for today's email yet", []
    by_rcpt = {}
    for e in events:
        by_rcpt.setdefault(e["email"].lower(), []).append(e)
    delivered, bounced, pending, private = 0, 0, 0, []
    for rcpt, evs in sorted(by_rcpt.items()):
        kinds = {e["event"] for e in evs}
        if kinds & DELIVERED:
            delivered += 1
        elif kinds & FAILED:
            bounced += 1
            why = next((e.get("reason") for e in evs if e["event"] in FAILED and e.get("reason")), "")
            kind = next(k for k in kinds if k in FAILED)
            private.append(f"{rcpt}: {kind}{' - ' + why if why else ''}")
        else:
            pending += 1
            private.append(f"{rcpt}: not delivered yet ({', '.join(sorted(kinds))})")
    total = len(by_rcpt)
    summary = f"delivered {delivered}/{total}" + (f", bounced {bounced}" if bounced else "") + \
              (f", pending {pending}" if pending else "")
    status = RED if bounced else AMBER if pending else GREEN
    return status, summary, private


# ── one automation, end to end ─────────────────────────────────────────────────────────────

def watch(job, c, cfg, dry_run=False, allow_local=False, wait_minutes=12):
    tz = ZoneInfo(cfg["timezone"])
    res = Result(job["id"], job["name"])
    now = c.now()
    try:
        if job.get("hold_variable"):
            held = (c.github.variable(job["repo"], job["hold_variable"]) or "").strip().lower() == "true"
            if held:
                res.worsen(AMBER)
                res.detail = f"held on purpose ({job['hold_variable']}=true)"
                return res
        done, detail = check_done(job, c.github, now, tz)
        res.detail = detail
        probs = run_problems(job, c.github, now, tz)
    except BillingBlocked as err:
        res.worsen(RED, "GitHub billing block: Actions refuses to start jobs - fix Billing & plans")
        res.detail = "cannot check: billing blocked"
        return res
    except ActionsUnavailable as err:
        res.worsen(RED, f"GitHub unavailable: {err}")
        res.detail = "cannot check: GitHub unavailable"
        return res

    if probs["billing"]:
        res.notes.append(f"{probs['billing']} run(s) today refused by GitHub billing")
    if probs["startup_failure"]:
        res.notes.append(f"{probs['startup_failure']} run(s) today failed to start (startup_failure)")
    if probs["skipped"]:
        res.notes.append(f"{probs['skipped']} run(s) today skipped/cancelled")
    if probs["failed"]:
        res.notes.append(f"{probs['failed']} run(s) today failed")

    if done is None:                                   # not due yet
        if probs["billing"]:
            res.worsen(RED)
        return res

    if not done:
        cutoff = job.get("cutoff")
        today = now.astimezone(tz).date()
        if cutoff and now >= at(today, cutoff, tz):
            res.worsen(RED)
            res.action = f"past the {cutoff} cutoff - not re-triggered. Manual: {job['manual']}"
        elif dry_run:
            res.worsen(RED)
            res.action = "dry run - would re-trigger"
        else:
            done = recover(job, res, c, cfg, allow_local, wait_minutes, tz)

    if done:
        if probs["billing"] or probs["startup_failure"]:
            res.worsen(AMBER)                          # recovered, but something went wrong first
        mail_status, mail_summary, private = delivery(job, c.brevo, c.now(), tz)
        if mail_summary:
            res.detail += f"; email {mail_summary}"
        res.private += private
        res.worsen(mail_status)
    elif res.status != RED:
        res.worsen(RED)
    return res


def recover(job, res, c, cfg, allow_local, wait_minutes, tz):
    """Re-trigger the job, wait for it, re-check. Falls back to a local run if Actions can't."""
    try:
        c.github.dispatch(job["repo"], job["workflow"], cfg.get("ref", "master"))
        res.action = "re-triggered (workflow_dispatch)"
    except RuntimeError as err:                       # token cannot run it: say so, never "re-triggered"
        res.worsen(RED)
        res.action = f"re-trigger refused: {err}. Manual: {job['manual']}"
        return False
    except (ActionsUnavailable, BillingBlocked) as err:
        why = "billing blocked" if isinstance(err, BillingBlocked) else "Actions unavailable"
        cmd = job.get("local_command")
        if cmd and allow_local:
            code = c.runner.run(cmd)
            res.action = f"{why} - ran locally (exit {code})"
            res.worsen(AMBER if code == 0 else RED)
            return code == 0
        res.worsen(RED)
        res.action = f"{why} - could not re-trigger. Manual: {job['manual']}"
        return False

    deadline = c.now() + timedelta(minutes=wait_minutes)
    while c.now() < deadline:
        c.sleep(30)
        try:
            done, detail = check_done(job, c.github, c.now(), tz)
        except (ActionsUnavailable, BillingBlocked):
            continue
        if done:
            res.detail = detail
            res.action += " - recovered"
            res.worsen(AMBER)
            return True
    res.worsen(RED)
    res.action += f" - still not done after {wait_minutes} min. Manual: {job['manual']}"
    return False


# ── digest ────────────────────────────────────────────────────────────────────────────────

COLOURS = {GREEN: "#2e7d32", AMBER: "#ef6c00", RED: "#d32f2f"}


def digest(results, actions_status, now, tz):
    n = {s: sum(r.status == s for r in results) for s in (GREEN, AMBER, RED)}
    day = now.astimezone(tz)
    subject = f"NMG watchdog {day:%d/%m/%Y} - {n[RED]} red, {n[AMBER]} amber, {n[GREEN]} green"
    head = "" if actions_status in ("operational", "unknown") else \
        f"GitHub Actions status: {actions_status.replace('_', ' ')}"
    rows, text = [], [subject, ""] + ([head, ""] if head else [])
    for r in sorted(results, key=lambda r: -RANK[r.status]):
        extra = "<br>".join(html.escape(x) for x in r.notes + r.private)
        rows.append(
            f"<tr><td style='padding:6px 10px'><span style='display:inline-block;width:12px;height:12px;"
            f"border-radius:6px;background:{COLOURS[r.status]}'></span> <b>{r.status.upper()}</b></td>"
            f"<td style='padding:6px 10px'><b>{html.escape(r.name)}</b></td>"
            f"<td style='padding:6px 10px'>{html.escape(r.detail)}"
            f"{'<br><i>' + html.escape(r.action) + '</i>' if r.action else ''}"
            f"{'<br><small>' + extra + '</small>' if extra else ''}</td></tr>")
        text.append(f"[{r.status.upper()}] {r.name}: {r.detail}" + (f" | {r.action}" if r.action else ""))
        text += [f"    {x}" for x in r.notes + r.private]
    body = (f"<div style='font-family:Segoe UI,Arial,sans-serif;font-size:14px'><h3>{html.escape(subject)}</h3>"
            + (f"<p style='color:#d32f2f'><b>{html.escape(head)}</b></p>" if head else "")
            + "<table style='border-collapse:collapse' border='1' bordercolor='#ddd'>" + "".join(rows)
            + f"</table><p style='color:#777'>Checked {day:%H:%M} AWST by nmg-watchdog.</p></div>")
    return subject, body, "\n".join(text)


def load_config(path=CONFIG):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def run(cfg, c, dry_run=False, allow_local=False, wait_minutes=12, send=True, env=None):
    env = env if env is not None else os.environ
    tz = ZoneInfo(cfg["timezone"])
    actions_status = c.status.actions_status()
    results = [watch(job, c, cfg, dry_run, allow_local, wait_minutes) for job in cfg["automations"]]
    subject, html_body, text_body = digest(results, actions_status, c.now(), tz)
    for r in results:                                   # logs: no addresses
        print(f"{r.status.upper():5} {r.id}: {r.detail}" + (f" | {r.action}" if r.action else ""))
    if send and not dry_run:
        to = env.get("DIGEST_TO")
        if not to:
            raise RuntimeError("DIGEST_TO is not set")
        how = c.mailer.send(to, env.get("DIGEST_FROM", DEFAULT_FROM), subject, html_body, text_body)
        print(f"digest sent ({how})")
    return results, subject, text_body


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--dry-run", action="store_true", help="check only: no dispatch, no local run, no email")
    ap.add_argument("--allow-local", action="store_true", help="run configured local_command when Actions can't")
    ap.add_argument("--wait-minutes", type=int, default=12)
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    env = os.environ
    if not env.get("WATCHDOG_TOKEN"):
        # Without it the private repos read as "not found" and every check would be wrong.
        msg = "WATCHDOG_TOKEN is not set - the watchdog cannot see the private repos, so nothing was checked."
        print(msg)
        if not (args.dry_run or args.no_email) and env.get("DIGEST_TO"):
            Mailer(env).send(env["DIGEST_TO"], env.get("DIGEST_FROM", DEFAULT_FROM),
                             "NMG watchdog - NOT RUNNING: WATCHDOG_TOKEN missing",
                             f"<p style='color:#d32f2f'><b>{msg}</b></p><p>Add the secret on "
                             "github.com/neil-noble/nmg-watchdog - Settings - Secrets - Actions.</p>", msg)
        return 1
    c = Clients(github=GitHub(env.get("WATCHDOG_TOKEN"), cfg["owner"]), status=GitHubStatus(),
                brevo=Brevo(env["BREVO_API_KEY"]) if env.get("BREVO_API_KEY") else None,
                mailer=Mailer(env), runner=LocalRunner(), now=lambda: datetime.now(timezone.utc))
    run(cfg, c, args.dry_run, args.allow_local, args.wait_minutes, send=not args.no_email)
    return 0


if __name__ == "__main__":
    sys.exit(main())
