#!/usr/bin/env python3
"""automation_sweep.py — append one automation-health snapshot to status_log.jsonl.

HuB is a read-only dashboard: it fetches `status_log.jsonl` over the public CDN
(raw.githubusercontent.com) and renders it client-side. It cannot call the
GitHub API itself. The unauthenticated budget is 60 requests/hour *per visitor
IP*, and one page load that fetched comments would spend about a third of it —
breaking the page for everyone behind a shared address. So anything the
dashboard shows must be precomputed into this file by a scheduled job.

This script is that job. It answers "is the agent automation actually working?"
from data that is already public:

  * run activity — run-ids appear verbatim in agent comments
    (`run=YYYYMMDD-HHMM-xxxx`) across the tracked repos. A run-id encodes its
    own UTC start time, so activity needs no extra API call to date.
  * runs per day — the same run-ids bucketed by UTC day over a 30-day window,
    so the dashboard can chart throughput rather than a single snapshot number.
  * run durations — a run's start comes from its id; its end is the first
    comment that carries the same id *and* a done/closed/completed word. A run
    with no such comment has an unmeasured duration and is left absent, never
    recorded as zero.
  * recent runs — the newest runs with repo, issue number, duration and outcome,
    each linking to the issue that carries its comments.
  * real WIP — `claims/N.claim` files, which are the actual lock. The published
    `flow.wip` counts the `status:claimed` *label* instead, so it reads 0 while
    work is in flight. This block reports the claim files as well.
  * stale claims — a claim file whose issue is already CLOSED. Nothing releases
    the lock when an issue closes, so these accumulate and will mislead any
    claim-counting reader.
  * the queue — every open issue's labels, counted per repo as available /
    claimed / blocked-needs-input, with the issue numbers behind each count. The
    audit queue (`on-hold`, `auditor:*`) is counted separately, because it is
    the human-promotion gate and not a "needs input" item.
  * needs-you — every blocked-needs-input or needs:human issue across all repos,
    oldest first: the maintainer's action list.
  * last run — the most recent run-id seen, with its start time.

Deliberately absent: failure rate and idle rate. A failed run dies before it
comments, so it leaves no public trace; only runs that got far enough to write
a comment are visible. Those numbers need the automation API and a credential,
which a static public page cannot hold. An absent field is a true statement; a
guessed zero is not.

One deliberate exception to the absent-vs-zero rule: `runs_by_day` carries a
full 30-day series with explicit zeros. The sweep fetches the whole window, so
a zero there is a measured "no run that day", not an unmeasured blank. Every
other new field is omitted when it was not measured.

Design rules (same as the source repos' hub_sweep.py):
  * Append only. One added line per sweep; existing lines never rewritten.
  * Honest numbers only — omit what cannot be computed. A 401 is raised rather
    than silently reported as "zero runs".
  * No duplicate snapshots unless --force.

Usage:
    python3 automation_sweep.py --selftest
    python3 automation_sweep.py --dry-run
    python3 automation_sweep.py

It writes two files, both fetched by the dashboard over the public CDN:
  * status_log.jsonl — append-only history, one small summary line per change.
  * work_board.json — the current state: per-day runs, run durations, the
    queue, and the "needs you" list. Rewritten in place, never appended, so the
    fetched size stays flat instead of growing without bound.

Exit codes: 0 wrote (or dry-run printed) a snapshot; 1 a real error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

LOG_NAME = "status_log.jsonl"
BOARD_NAME = "work_board.json"
API = "https://api.github.com"
OWNER = "philipdallen"
# Repos where agents claim issues and leave run-id comments.
SOURCE_REPOS = ("ephapse", "PleaNP", "rubato", "Maith")
# Repos whose issue queues are part of the board. HuB is here — its own
# blocked-needs-input issue is exactly what the "needs you" panel is for — but
# it is not in SOURCE_REPOS, because HuB agents do not write run-id comments,
# so a runs row for it would be a measurement artifact rather than a real zero.
QUEUE_REPOS = SOURCE_REPOS + ("HuB",)

# A run-id is <YYYYMMDD>-<HHMM>-<4 random alphanumerics>, per the contract.
# Real comments reference the id in five shapes, all of which mean the same run:
#   run=20261019-1502-h3n7    run=`20261019-1502-h3n7`   (bare / Markdown code)
#   run 20261019-1502-h3n7    run `20261019-1502-h3n7`   (prose, no "=")
#   _run=20261019-1502-h3n7_                             (Markdown italic)
# A pattern accepting only the first silently drops the rest — losing the run
# and its outcome. The lookbehind requires "run" to start a word but allows the
# Markdown wrappers (`*_); across 30 days of comments the only characters before
# a match are space, "(", newline, "`", "*", "_" — never a letter or digit, so
# "rerun=..." cannot match. The id shape itself is too specific for prose.
RUN_RE = re.compile(r"(?<![A-Za-z0-9])run\s*=?\s*`?(\d{8})-(\d{4})-([a-z0-9]{4})")
RUN_ID_RE = re.compile(r"(\d{8})-(\d{4})-([a-z0-9]{4})")
CLAIM_FILE_RE = re.compile(r"(\d+)\.claim")
# A claim comment opens with "claimed" ("claimed by openhands run=..." or
# "claimed #109 by ..."). A done comment names a completion in prose.
CLAIM_COMMENT_RE = re.compile(r"^\s*claimed\b", re.IGNORECASE)
DONE_COMMENT_RE = re.compile(r"\b(done|closed|completed)\b", re.IGNORECASE)

# Windows the sweep fetches. Runs per day needs 30 days; the queue is a live
# snapshot. Caps bound the API cost of a job that runs every 30 minutes.
WINDOW_DAYS = 30
COMMENT_PAGE_CAP = 20   # 2000 comments/repo; rubato's 30-day peak was ~1300
ISSUE_PAGE_CAP = 3      # 300 open issues/repo; rubato's peak was 48
RECENT_RUNS = 20        # the dashboard shows the last ~15


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def run_start(run_id: str) -> dt.datetime | None:
    """The UTC start a run-id encodes, or None if it is not a valid id."""
    m = RUN_ID_RE.fullmatch(run_id or "")
    if not m:
        return None
    try:
        start = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M")
    except ValueError:
        return None
    return start.replace(tzinfo=dt.timezone.utc)


def parse_run(text: str) -> tuple[str, dt.datetime] | None:
    """Return (run_id, start_time_utc) for the first run-id in `text`.

    A run-id carries its own start time, so the comment's created_at is not
    needed to date it. Returns None when absent or not a valid timestamp.
    """
    m = RUN_RE.search(text or "")
    if not m:
        return None
    run_id = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    start = run_start(run_id)
    return None if start is None else (run_id, start)


def classify_comment(body: str) -> str:
    """'claim' | 'done' | 'other'.

    A claim comment opens with "claimed". A done comment carries a completion
    word. Claim is tested first so a claim comment's prose cannot also read as
    a completion.
    """
    text = body or ""
    if CLAIM_COMMENT_RE.match(text):
        return "claim"
    if DONE_COMMENT_RE.search(text):
        return "done"
    return "other"


def _to_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(when: dt.datetime) -> str:
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _get(url: str, token: str | None) -> tuple[int, object]:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "hub-automation-sweep")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 404, None
        # Never let an auth or rate-limit failure look like "no activity".
        if exc.code in (401, 403):
            raise RuntimeError(
                f"HTTP {exc.code} from {url} — token rejected or rate limited; "
                f"refusing to publish a zero"
            ) from exc
        return exc.code, None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"GET {url} failed: {exc}") from exc


def fetch_activity(repo: str, token: str | None, since: dt.datetime) -> list[dict] | None:
    """Comment records carrying a run-id, for this repo, since `since`.

    Each record is {"issue", "created_at", "run_id", "kind"}. Returns None when
    the first page failed outright, so the caller can omit the repo rather than
    publish a false zero for it.
    """
    out: list[dict] = []
    page = 1
    while page <= COMMENT_PAGE_CAP:
        url = (f"{API}/repos/{OWNER}/{repo}/issues/comments"
               f"?per_page=100&page={page}&since={since.isoformat()}")
        status, body = _get(url, token)
        if status != 200 or not isinstance(body, list):
            return None if page == 1 else out
        if not body:
            break
        for c in body:
            parsed = parse_run(c.get("body") or "")
            if not parsed or parsed[1] < since:
                continue
            try:
                issue = int(str(c.get("issue_url", "")).rsplit("/", 1)[-1])
            except ValueError:
                issue = None
            out.append({
                "issue": issue,
                "created_at": c.get("created_at"),
                "run_id": parsed[0],
                "kind": classify_comment(c.get("body") or ""),
            })
        if len(body) < 100:
            break
        page += 1
    return out


def fetch_claims(repo: str, token: str | None) -> list[int] | None:
    """Issue numbers with a claim file. None when the repo has no claims/ dir,
    which is different from an empty one: absent mechanism vs zero claims."""
    status, body = _get(f"{API}/repos/{OWNER}/{repo}/contents/claims", token)
    if status == 404:
        return None
    if status != 200 or not isinstance(body, list):
        return None
    out = []
    for entry in body:
        m = CLAIM_FILE_RE.fullmatch(entry.get("name") or "")
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def issue_state(repo: str, number: int, token: str | None) -> str | None:
    status, body = _get(f"{API}/repos/{OWNER}/{repo}/issues/{number}", token)
    if status == 200 and isinstance(body, dict):
        return body.get("state")
    return None


def fetch_issues(repo: str, token: str | None) -> list[dict] | None:
    """Open issues with their labels (pull requests excluded).

    Returns None on first-page failure so a repo that could not be read is
    omitted rather than shown as an empty queue. Titles and labels are
    untrusted and must be escaped by the renderer.
    """
    out: list[dict] = []
    page = 1
    while page <= ISSUE_PAGE_CAP:
        status, body = _get(
            f"{API}/repos/{OWNER}/{repo}/issues?state=open&per_page=100&page={page}", token)
        if status != 200 or not isinstance(body, list):
            return None if page == 1 else out
        if not body:
            break
        for i in body:
            if "pull_request" in i:
                continue
            out.append({
                "number": i.get("number"),
                "title": i.get("title") or "",
                "labels": sorted(l.get("name") for l in (i.get("labels") or [])
                                 if l.get("name")),
                "created_at": i.get("created_at"),
            })
        if len(body) < 100:
            break
        page += 1
    return out


def collect_runs(activity_by_repo: dict[str, list[dict]]) -> dict[str, dict]:
    """Merge per-repo comment records into one entry per run-id.

    A run-id seen in two repos is one run — the same dedup rule the original
    sweep used. `repos` keeps every repo the id appeared in, so the per-repo
    chart still counts it where the work happened. `done` is the earliest
    completion comment at or after the run's start; a run with no such comment
    keeps `done: None`, which is an unmeasured duration, not a zero.
    """
    runs: dict[str, dict] = {}
    for repo, records in (activity_by_repo or {}).items():
        for rec in records or []:
            rid = rec.get("run_id")
            start = run_start(rid)
            if start is None:
                continue
            entry = runs.setdefault(rid, {"start": start, "repos": set(),
                                          "issue": None, "done": None})
            entry["repos"].add(repo)
            if rec.get("kind") == "claim" and rec.get("issue") is not None:
                entry["issue"] = rec["issue"]
            elif entry["issue"] is None:
                entry["issue"] = rec.get("issue")
            if rec.get("kind") == "done":
                when = _to_dt(rec.get("created_at"))
                if when is not None and when >= start and (
                        entry["done"] is None or when < entry["done"]):
                    entry["done"] = when
    return runs


def claim_times(activity_by_repo: dict[str, list[dict]]) -> dict[tuple[str, int], dt.datetime]:
    """{(repo, issue): start} for the most recent claim comment on that issue.

    The claim comment carries the run-id that claimed it, whose id encodes the
    claim time — so claim age needs no timeline API call.
    """
    out: dict[tuple[str, int], dt.datetime] = {}
    for repo, records in (activity_by_repo or {}).items():
        for rec in records or []:
            if rec.get("kind") != "claim" or rec.get("issue") is None:
                continue
            start = run_start(rec.get("run_id"))
            if start is None:
                continue
            key = (repo, rec["issue"])
            if key not in out or start > out[key]:
                out[key] = start
    return out


def build_runs(activity_by_repo: dict[str, list[dict]], repos_scanned: list[str],
               now: dt.datetime) -> dict:
    """Runs per day, measured durations, and the recent-runs table."""
    runs = collect_runs(activity_by_repo)
    days = [(now - dt.timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(WINDOW_DAYS - 1, -1, -1)]
    floor = now - dt.timedelta(days=WINDOW_DAYS)

    by_day = {repo: {d: 0 for d in days} for repo in repos_scanned}
    for rid, e in runs.items():
        if e["start"] < floor:
            continue
        day = e["start"].strftime("%Y-%m-%d")
        for repo in e["repos"]:
            if repo in by_day:
                by_day[repo][day] += 1

    minutes_by_day = {d: 0.0 for d in days}
    measured = False
    recent = []
    for rid, e in runs.items():
        if e["start"] < floor:
            continue
        minutes = None
        if e["done"] is not None:
            delta = (e["done"] - e["start"]).total_seconds() / 60.0
            if delta >= 0:
                minutes = round(delta, 1)
                minutes_by_day[e["start"].strftime("%Y-%m-%d")] += minutes
                measured = True
        recent.append({
            "run_id": rid,
            "repo": sorted(e["repos"])[0],
            "start": _iso(e["start"]),
            "issue": e["issue"],
            "minutes": minutes,
            "outcome": "done" if e["done"] is not None else None,
        })
    recent.sort(key=lambda r: r["start"], reverse=True)

    # The header strip shows "runs in the last 24h" per repo. This is a rolling
    # 24 hours, which a day-bucketed series cannot express (a day boundary cuts
    # it), so it is computed here rather than summed from runs_by_day.
    day = now - dt.timedelta(hours=24)
    runs_24h = {repo: 0 for repo in repos_scanned}
    for e in runs.values():
        if e["start"] < day:
            continue
        for repo in e["repos"]:
            if repo in runs_24h:
                runs_24h[repo] += 1

    out = {"runs_by_day": by_day, "runs_24h_by_repo": runs_24h,
           "recent_runs": recent[:RECENT_RUNS]}
    if measured:
        out["agent_minutes_by_day"] = {d: round(v, 1) for d, v in minutes_by_day.items()}
    return out


def build_queue(issues_by_repo: dict[str, list[dict] | None],
                claimed_at: dict[tuple[str, int], dt.datetime],
                now: dt.datetime) -> tuple[dict, list[dict]]:
    """Per-repo queue counts and the cross-repo 'needs you' action list.

    `issues_by_repo` holds only OPEN issues (fetch_issues queries state=open),
    so a closed issue still carrying a stale `status:` label is excluded here by
    construction rather than by a filter that could be forgotten.
    """
    queue: dict[str, dict] = {}
    needs: list[dict] = []
    for repo, issues in (issues_by_repo or {}).items():
        if issues is None:
            continue
        avail, claimed, blocked, hold = [], [], [], []
        for i in issues:
            ls = set(i.get("labels") or [])
            n = i.get("number")
            if n is None:
                continue
            if "status:available" in ls:
                avail.append(n)
            if "status:claimed" in ls:
                claimed.append(n)
            if "status:blocked-needs-input" in ls:
                blocked.append(n)
            if "on-hold" in ls:
                hold.append(n)
            if "status:blocked-needs-input" in ls or "needs:human" in ls:
                needs.append({
                    "repo": repo,
                    "number": n,
                    "title": i.get("title") or "",
                    "labels": i.get("labels") or [],
                    "opened": (i.get("created_at") or "")[:10],
                })
        entry = {
            "available": len(avail),
            "claimed": len(claimed),
            "blocked_needs_input": len(blocked),
            "on_hold": len(hold),
            "available_issues": sorted(avail),
            "blocked_issues": sorted(blocked),
        }
        if claimed:
            entry["claimed_issues"] = [
                {"number": n, "claimed_at": _iso(claimed_at[(repo, n)])}
                if (repo, n) in claimed_at else {"number": n}
                for n in sorted(claimed)
            ]
        queue[repo] = entry

    # Oldest first: the longest-waiting item is the one that rots.
    needs.sort(key=lambda e: (e["opened"], e["repo"], e["number"]))
    return queue, needs


def build_automation(activity_by_repo: dict[str, list[dict]],
                     claims_by_repo: dict[str, list[int] | None],
                     states: dict[tuple[str, int], str | None],
                     now: dt.datetime,
                     issues_by_repo: dict[str, list[dict] | None] | None = None) -> dict:
    """The small per-snapshot summary appended to status_log.jsonl.

    Deliberately small: this log is append-only and fetched whole by the
    browser, so a ~12 KB line would grow the file by tens of MB a year and
    break the page. The detailed current-state blocks live in work_board.json
    (build_work_board), which is rewritten in place and never accumulates.
    """
    runs = collect_runs(activity_by_repo)
    all_runs = {rid: e["start"] for rid, e in runs.items()}

    day = now - dt.timedelta(hours=24)
    week = now - dt.timedelta(days=7)

    active = sorted({repo for e in runs.values() if e["start"] >= day
                     for repo in e["repos"]})

    # Only repos that actually have the claims/ mechanism are counted; a repo
    # without it would otherwise contribute a false zero.
    held = {repo: len(nums) for repo, nums in claims_by_repo.items() if nums is not None}

    stale = []
    for repo, nums in claims_by_repo.items():
        for n in nums or []:
            if states.get((repo, n)) == "closed":
                stale.append(f"{repo} #{n}")

    out: dict = {
        "runs_24h": sum(1 for ts in all_runs.values() if ts >= day),
        "runs_7d": sum(1 for ts in all_runs.values() if ts >= week),
        "claims_total": sum(held.values()),
        "stale_claims_total": len(stale),
    }
    if active:
        out["repos_active_24h"] = active
    if held:
        out["claims_held"] = held
    if stale:
        out["stale_claims"] = sorted(stale)
    if all_runs:
        last = max(all_runs, key=lambda r: all_runs[r])
        out["last_run_id"] = last
        out["last_run"] = _iso(all_runs[last])

    # Issue #13: a falling run count reads as a broken scheduler when it is
    # really an empty queue. One integer here lets a reader tell those apart
    # without fetching the full queue block.
    if issues_by_repo is not None:
        queue, _ = build_queue(issues_by_repo, claim_times(activity_by_repo), now)
        if queue:
            out["available_total"] = sum(q["available"] for q in queue.values())
    return out


def build_work_board(activity_by_repo: dict[str, list[dict]],
                     issues_by_repo: dict[str, list[dict] | None],
                     claims_by_repo: dict[str, list[int] | None],
                     states: dict[tuple[str, int], str | None],
                     now: dt.datetime) -> dict:
    """The dashboard's current state: throughput, durations, queue, needs-you.

    This is the block that carries the records behind every number, so each
    value can be drilled into. It is a single current-state document, rewritten
    in place by the sweep — not an append-only log — because duplicating it into
    each history line would grow the fetched file without bound.
    """
    scanned = sorted(repo for repo, records in activity_by_repo.items()
                     if records is not None)
    board: dict = {"generated": _iso(now), "repos": scanned}
    board.update(build_runs(activity_by_repo, scanned, now))

    # Only repos with a claims/ mechanism are counted; a repo without one would
    # contribute a false zero, the same rule the log summary follows. When no
    # repo has the mechanism, the block is empty rather than "0 held" — zero
    # claim files is not the same statement as zero work in flight.
    held = {repo: len(nums) for repo, nums in claims_by_repo.items() if nums is not None}
    stale = [f"{repo} #{n}" for repo, nums in claims_by_repo.items()
             for n in (nums or []) if states.get((repo, n)) == "closed"]
    claims: dict = {}
    if held:
        claims["total"] = sum(held.values())
        claims["held"] = held
    if stale:
        claims["stale"] = sorted(stale)
    board["claims"] = claims

    if issues_by_repo is not None:
        queue, needs = build_queue(issues_by_repo, claim_times(activity_by_repo), now)
        if queue:
            board["queue"] = queue
        if needs:
            board["needs_you"] = needs
    return board


def build_notes(automation: dict) -> str:
    parts = [f"automation: {automation['runs_24h']} run(s)/24h, "
             f"{automation['runs_7d']} run(s)/7d"]
    if automation.get("repos_active_24h"):
        parts.append("active: " + ", ".join(automation["repos_active_24h"]))
    if automation.get("claims_total"):
        parts.append(f"{automation['claims_total']} claim(s) held")
    if automation.get("stale_claims_total"):
        parts.append(f"{automation['stale_claims_total']} STALE claim(s) "
                     f"({', '.join(automation.get('stale_claims', []))})")
    if "available_total" in automation:
        parts.append(f"{automation['available_total']} available")
    if automation.get("last_run"):
        parts.append(f"last run {automation['last_run']}")
    return " | ".join(parts)


def build_snapshot(activity_by_repo, claims_by_repo, states, now=None,
                   issues_by_repo=None) -> dict:
    now = now or utcnow()
    automation = build_automation(activity_by_repo, claims_by_repo, states, now,
                                  issues_by_repo)
    return {
        "timestamp": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "automation": automation,
        "notes": build_notes(automation),
    }


# --------------------------------------------------------------------------
# Appending (same shape as the source repos' hub_sweep.py)
# --------------------------------------------------------------------------

def read_last_snapshot(path: Path) -> dict | None:
    if not path.exists():
        return None
    last = None
    for line in path.read_text().splitlines():
        if line.strip():
            last = line
    if not last:
        return None
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        return None


def should_append(snapshot: dict, previous: dict | None, force: bool) -> bool:
    if force or previous is None:
        return True
    return snapshot.get("automation") != previous.get("automation")


def append(path: Path, snapshot: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    separator = "" if (not existing or existing.endswith("\n")) else "\n"
    with path.open("a") as fh:
        fh.write(separator + json.dumps(snapshot, sort_keys=True) + "\n")


# --------------------------------------------------------------------------

def selftest() -> int:
    """Prove the pure logic can fail. A check that cannot fail is not a check."""
    cases: list[tuple[str, bool]] = []

    got = parse_run("claimed by openhands run=20260923-0523-ez4n at 2026-09-23T05:24Z.")
    cases.append(("parse_run reads a valid id", got is not None and got[0] == "20260923-0523-ez4n"))
    cases.append(("parse_run dates from the id itself",
                  got is not None and got[1] == dt.datetime(2026, 9, 23, 5, 23, tzinfo=dt.timezone.utc)))
    cases.append(("parse_run ignores text without an id", parse_run("no run id here") is None))
    # Real comments reference an id in five shapes; all five are the same run.
    # A pattern accepting only the bare "=" form drops the rest and loses the
    # run and its outcome — see the RUN_RE note.
    for label, text in (
        ("bare", "run=20261019-1502-h3n7"),
        ("backticked", "run=`20261019-1502-h3n7`"),
        ("space", "Done by run 20261019-1502-h3n7 — commit abc."),
        ("space-backticked", "Swept by run `20261019-1502-h3n7`."),
        ("italic", "_run=20261019-1502-h3n7_"),
    ):
        got = parse_run(text)
        cases.append((f"parse_run reads the {label} id form",
                      got is not None and got[0] == "20261019-1502-h3n7"))
    cases.append(("parse_run does not read a bare id as a run reference",
                  parse_run("the id 20261019-1502-h3n7 appears with no word before it") is None))
    cases.append(("parse_run does not read 'rerun=' as a run reference",
                  parse_run("a rerun=20261019-1502-h3n7 was skipped") is None))
    cases.append(("parse_run dates a backticked id from the id itself",
                  (lambda p: p is not None and p[1] == dt.datetime(2026, 10, 19, 15, 2, tzinfo=dt.timezone.utc))(
                      parse_run("**Done** — run=`20261019-1502-h3n7`, branch `main`."))))
    cases.append(("parse_run rejects an out-of-range month", parse_run("run=20261399-0000-aaaa") is None))
    cases.append(("parse_run rejects a short suffix", parse_run("run=20260923-0523-ab") is None))

    # classify_comment: a claim line is a claim, prose completion is done.
    cases.append(("classify: claim line", classify_comment("claimed by openhands run=20260923-0523-ez4n") == "claim"))
    cases.append(("classify: claim by issue number", classify_comment("claimed #109 by openhands run=20260923-0523-ez4n") == "claim"))
    cases.append(("classify: done prose", classify_comment("Done — run=20260923-0523-ez4n, commit abc123.") == "done"))
    cases.append(("classify: closed prose", classify_comment("Closed by run=20260923-0523-ez4n.") == "done"))
    cases.append(("classify: neither", classify_comment("heartbeat for run=20260923-0523-ez4n") == "other"))
    # A claim comment whose prose also says "done" must still read as a claim,
    # or the run's start time would be taken from the wrong comment.
    cases.append(("classify: claim beats a stray done word",
                  classify_comment("claimed by openhands run=20260923-0523-ez4n — not done yet") == "claim"))

    now = dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.timezone.utc)

    def rec(run_id, kind, issue=1, created=None):
        return {"run_id": run_id, "kind": kind, "issue": issue,
                "created_at": created or now.replace(microsecond=0).isoformat().replace("+00:00", "Z")}

    activity = {
        "ephapse": [
            rec("20260923-1000-aaaa", "claim", issue=39, created="2026-09-23T10:00:30Z"),
            rec("20260923-1000-aaaa", "done", issue=39, created="2026-09-23T10:20:30Z"),
            rec("20260920-1000-bbbb", "claim", issue=45, created="2026-09-20T10:00:30Z"),
        ],
        "Maith": [],
    }
    claims = {"ephapse": [39, 45], "Maith": None}
    states = {("ephapse", 39): "closed", ("ephapse", 45): "open"}
    issues = {
        "ephapse": [
            {"number": 39, "title": "stale lock", "labels": ["status:claimed"],
             "created_at": "2026-09-01T00:00:00Z"},
            {"number": 45, "title": "waiting on you", "labels": ["status:blocked-needs-input", "needs:human"],
             "created_at": "2026-09-02T00:00:00Z"},
            {"number": 46, "title": "claimable", "labels": ["status:available"],
             "created_at": "2026-09-03T00:00:00Z"},
            {"number": 47, "title": "parked", "labels": ["on-hold", "auditor:proposed"],
             "created_at": "2026-09-04T00:00:00Z"},
            {"number": 44, "title": "needs you, but newer", "labels": ["status:blocked-needs-input"],
             "created_at": "2026-09-05T00:00:00Z"},
        ],
        "Maith": None,
    }

    a = build_automation(activity, claims, states, now, issues)
    cases.append(("runs_24h counts only the recent run", a["runs_24h"] == 1))
    cases.append(("runs_7d counts both", a["runs_7d"] == 2))
    cases.append(("claims_total counts claim files", a["claims_total"] == 2))
    cases.append(("stale_claims flags a closed issue's lock", a["stale_claims"] == ["ephapse #39"]))
    cases.append(("stale_claims spares an open issue's lock", "ephapse #45" not in a["stale_claims"]))
    cases.append(("a repo without claims/ is excluded, not zeroed", "Maith" not in a["claims_held"]))
    cases.append(("last_run picks the newest id", a["last_run_id"] == "20260923-1000-aaaa"))
    cases.append(("repos_active_24h lists the busy repo", a["repos_active_24h"] == ["ephapse"]))
    cases.append(("the appended snapshot stays small (no detail blocks)",
                  "queue" not in a and "recent_runs" not in a and "runs_by_day" not in a))
    cases.append(("the snapshot carries the one-number queue signal",
                  a["available_total"] == 1))

    # The work board is the current-state document the page leads with.
    board = build_work_board(activity, issues, claims, states, now)
    cases.append(("the work board is timestamped", board["generated"] == "2026-09-23T12:00:00Z"))
    cases.append(("the work board lists the scanned repos", board["repos"] == ["Maith", "ephapse"]))
    cases.append(("the board's claims block mirrors the log's",
                  board["claims"]["total"] == 2
                  and board["claims"]["held"] == {"ephapse": 2}
                  and board["claims"]["stale"] == ["ephapse #39"]))
    cases.append(("a repo with no claims/ mechanism is absent from held, not zero",
                  "Maith" not in board["claims"].get("held", {})))

    # Durations come from the done comment minus the id's encoded start.
    # The claim landed at 10:00:30 and the done at 10:20:30; the id encodes a
    # 10:00 start, so the measured duration is 20.5 minutes.
    recent = {r["run_id"]: r for r in board["recent_runs"]}
    cases.append(("a matched run reports its measured duration",
                  recent["20260923-1000-aaaa"]["minutes"] == 20.5))
    cases.append(("a matched run reports outcome done",
                  recent["20260923-1000-aaaa"]["outcome"] == "done"))
    cases.append(("an unmatched run leaves minutes absent, not 0",
                  recent["20260920-1000-bbbb"]["minutes"] is None))
    cases.append(("an unmatched run leaves outcome absent",
                  recent["20260920-1000-bbbb"]["outcome"] is None))
    cases.append(("the recent-runs row carries its issue number",
                  recent["20260923-1000-aaaa"]["issue"] == 39))
    cases.append(("agent_minutes_by_day only sums measured runs",
                  board["agent_minutes_by_day"].get("2026-09-23") == 20.5))
    cases.append(("runs_24h_by_repo counts the recent run once",
                  board["runs_24h_by_repo"] == {"Maith": 0, "ephapse": 1}))
    # A run just outside the rolling 24h must not be counted, even though it is
    # in the 30-day window and appears in runs_by_day.
    old = build_work_board({"ephapse": [rec("20260921-1000-aaaa", "claim")]}, {}, {}, {}, now)
    cases.append(("runs_24h_by_repo excludes a run older than 24h",
                  old["runs_24h_by_repo"] == {"ephapse": 0}))
    cases.append(("...but runs_by_day still counts it",
                  old["runs_by_day"]["ephapse"]["2026-09-21"] == 1))

    # runs_by_day: a full 30-day series with a measured zero on an idle day.
    # An empty list means "scanned, no runs"; None means "not scanned".
    days = board["runs_by_day"]["ephapse"]
    cases.append(("runs_by_day spans the full window", len(days) == WINDOW_DAYS))
    cases.append(("runs_by_day counts the day's runs", days.get("2026-09-23") == 1))
    cases.append(("runs_by_day records a measured zero on an idle day",
                  days.get("2026-09-22") == 0))
    cases.append(("a scanned repo with no runs still appears, all zeros",
                  "Maith" in board["runs_by_day"]
                  and set(board["runs_by_day"]["Maith"].values()) == {0}
                  and len(board["runs_by_day"]["Maith"]) == WINDOW_DAYS))
    d = build_work_board({"ephapse": [], "Maith": None}, {}, {}, {}, now)
    cases.append(("an unscanned repo (None) is omitted from runs_by_day",
                  "Maith" not in d["runs_by_day"]))

    # Queue counts and drill-down issue lists.
    q = board["queue"]["ephapse"]
    cases.append(("queue counts available", q["available"] == 1))
    cases.append(("queue counts blocked-needs-input", q["blocked_needs_input"] == 2))
    cases.append(("queue counts on-hold separately", q["on_hold"] == 1))
    cases.append(("queue lists the available issue number", q["available_issues"] == [46]))
    cases.append(("queue lists the blocked issue numbers", q["blocked_issues"] == [44, 45]))
    cases.append(("queue dates a claim from its claim comment",
                  q["claimed_issues"][0]["claimed_at"].startswith("2026-09-23T10:00")))
    cases.append(("a repo whose issues could not be fetched is omitted, not zeroed",
                  "Maith" not in board["queue"]))
    # A re-claim (a later claim comment on the same issue) must reset the age;
    # keeping the older timestamp would understate how long the lock has been
    # held and hide a stale claim.
    re_claim = build_work_board(
        {"ephapse": [rec("20260923-1000-aaaa", "claim", issue=9,
                         created="2026-09-23T10:00:30Z"),
                     rec("20260923-1100-zzzz", "claim", issue=9,
                         created="2026-09-23T11:00:30Z")]},
        {"ephapse": [{"number": 9, "title": "re-claimed", "labels": ["status:claimed"],
                      "created_at": "2026-09-01T00:00:00Z"}]}, {}, {}, now)
    cases.append(("a re-claim resets the claim age to the latest claim",
                  re_claim["queue"]["ephapse"]["claimed_issues"][0]["claimed_at"]
                  == "2026-09-23T11:00:00Z"))

    # needs_you is the cross-repo action list, oldest first.
    cases.append(("needs_you lists the blocked issues oldest first",
                  [e["number"] for e in board["needs_you"]] == [45, 44]))
    cases.append(("needs_you carries the title for the link",
                  board["needs_you"][0]["title"] == "waiting on you"))
    cases.append(("needs_you includes a needs:human-only issue",
                  build_work_board({"ephapse": []}, {"ephapse": [
                      {"number": 9, "title": "human only", "labels": ["needs:human"],
                       "created_at": "2026-09-01T00:00:00Z"}]}, {}, {}, now)["needs_you"][0]["number"] == 9))

    # No runs at all must not fabricate a last_run.
    b = build_automation({"ephapse": []}, {"ephapse": None}, {}, now)
    cases.append(("no runs -> no last_run field", "last_run" not in b))
    cases.append(("no runs -> runs_24h is 0", b["runs_24h"] == 0))
    bb = build_work_board({"ephapse": []}, {}, {}, {}, now)
    cases.append(("no measured durations -> no agent_minutes_by_day",
                  "agent_minutes_by_day" not in bb))
    cases.append(("no issues fetched -> no queue or needs_you in the board",
                  "queue" not in bb and "needs_you" not in bb))

    # Dedup: the same run id in two repos is one run.
    c = build_automation({"ephapse": [rec("20260923-1000-aaaa", "claim")],
                          "Maith": [rec("20260923-1000-aaaa", "claim")]},
                         {}, {}, now)
    cases.append(("a run id shared across repos counts once", c["runs_24h"] == 1))

    failed = [label for label, ok in cases if not ok]
    for label, ok in cases:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    if failed:
        print(f"\nSELF-TEST FAILED: {len(failed)} case(s): {failed}")
        return 1
    print(f"\nSELF-TEST OK: {len(cases)}/{len(cases)} cases behave as required")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append an automation snapshot for HuB.")
    parser.add_argument("--file", default=None,
                        help=f"log path (default: repo root / {LOG_NAME})")
    parser.add_argument("--board-file", default=None,
                        help=f"work-board path (default: repo root / {BOARD_NAME})")
    parser.add_argument("--token-env", default="GITHUB_TOKEN",
                        help="env var holding a token (optional for public repos)")
    parser.add_argument("--no-token", action="store_true",
                        help="force unauthenticated reads")
    parser.add_argument("--repos", default=",".join(SOURCE_REPOS),
                        help="comma-separated repos to scan for run activity")
    parser.add_argument("--queue-repos", default=",".join(QUEUE_REPOS),
                        help="comma-separated repos whose issue queues to read")
    parser.add_argument("--dry-run", action="store_true", help="print; write nothing")
    parser.add_argument("--force", action="store_true", help="append even if unchanged")
    parser.add_argument("--selftest", action="store_true", help="prove the logic can fail")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    repo_root = Path(os.environ.get("HUB_REPO_ROOT", ".")).resolve()
    log_path = Path(args.file) if args.file else repo_root / LOG_NAME
    board_path = Path(args.board_file) if args.board_file else repo_root / BOARD_NAME
    token = None if args.no_token else (os.environ.get(args.token_env) or None)
    repos = [r.strip() for r in args.repos.split(",") if r.strip()]
    queue_repos = [r.strip() for r in args.queue_repos.split(",") if r.strip()]

    now = utcnow()
    since = now - dt.timedelta(days=WINDOW_DAYS)
    activity_by_repo: dict[str, list[dict] | None] = {}
    claims_by_repo: dict[str, list[int] | None] = {}
    states: dict[tuple[str, int], str | None] = {}
    issues_by_repo: dict[str, list[dict] | None] = {}

    try:
        for repo in queue_repos:
            issues_by_repo[repo] = fetch_issues(repo, token)
        for repo in repos:
            activity_by_repo[repo] = fetch_activity(repo, token, since)
            nums = fetch_claims(repo, token)
            claims_by_repo[repo] = nums
            for n in nums or []:
                states[(repo, n)] = issue_state(repo, n, token)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    snapshot = build_snapshot(activity_by_repo, claims_by_repo, states, now,
                              issues_by_repo)
    board = build_work_board(activity_by_repo, issues_by_repo, claims_by_repo, states, now)
    previous = read_last_snapshot(log_path)

    if args.dry_run:
        print(json.dumps(snapshot, indent=2, sort_keys=True))
        print(json.dumps({"work_board": board}, indent=2, sort_keys=True))
        verb = "append" if should_append(snapshot, previous, args.force) else "skip (unchanged)"
        print(f"(dry-run: would {verb} to {log_path}; would write {board_path})")
        return 0

    if should_append(snapshot, previous, args.force):
        append(log_path, snapshot)
        print(f"appended to {log_path}: {snapshot['notes']}")
    else:
        print(f"unchanged since last snapshot; nothing appended to {log_path}")

    # The board is current state, not history: rewritten every sweep, so a
    # stale file is impossible and no reader can be shown yesterday's queue.
    board_path.write_text(json.dumps(board, indent=2, sort_keys=True) + "\n")
    print(f"wrote {board_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
