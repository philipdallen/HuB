#!/usr/bin/env python3
"""Validate config.json against the live repos it points at.

HuB's whole job is fetching status_log.jsonl from the URLs in config.json. A
typo in an owner, repo, or branch field silently degrades the dashboard to an
amber "no data" tab, and nothing currently fails. This script is that check.

Run: python3 tools/validate_config.py             # validate the real config
     python3 tools/validate_config.py --self-test # prove the check can fail
Exit 0 = every tracked repo resolves; 1 = at least one does not.

Why this resolves the branch rather than fetching the raw URL
------------------------------------------------------------
Fetching raw.githubusercontent.com/{owner}/{repo}/{branch}/status_log.jsonl
cannot detect a dead branch, and the earlier version of this script therefore
passed against branches that did not exist. Two separate causes, both verified
against this portfolio on 2026-09-22:

1. GitHub serves a deleted-or-never-existing branch lazily, from whatever
   content it last had. `ephapse` has no `dev` branch - the API agrees - yet
   raw.githubusercontent.com/philipdallen/ephapse/dev/status_log.jsonl returns
   200 with a byte-identical copy of main's log (md5 a7ae3448...). No redirect,
   no error. A stale branch name looks perfectly healthy.
2. /branches/{branch} itself cannot be used to check existence, because GitHub
   redirects it to the default branch: GET /branches/dev on `ephapse` returns
   301 -> /branches/main, so a naive `status == 200` after following redirects
   resolves a *nonexistent* branch to a *real* one and reports success. This is
   the same defect class the script exists to catch, so it is worth being
   explicit: the fix is NOT to stop following redirects.

The reliable check is GET /repos/{owner}/{repo}/git/ref/heads/{branch} - 404
when the branch is absent, 200 when present, no redirect. On success we then
fetch the log pinned to the resolved commit SHA, so the content check also
cannot be satisfied by a stale ref.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "config.json"
BOARD = Path(__file__).resolve().parent.parent / "work_board.json"
REQUIRED = ("name", "owner", "repo", "branch")

API = "https://api.github.com/repos/{owner}/{repo}/git/ref/heads/{branch}"
RAW = "https://raw.githubusercontent.com/{owner}/{repo}/{sha}/status_log.jsonl"
UA = {"User-Agent": "HuB-validate-config", "Accept": "application/vnd.github+json"}


def _get(url, headers=None):
    """GET a URL. Returns (status, body_text); status is None on a network error."""
    req = urllib.request.Request(url, headers=headers or UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, f"network error: {exc.reason}"


def load_config(path):
    """Parse config.json and assert its shape before anything touches the network."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        print(f"FAIL: {path} does not exist")
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"FAIL: {path} is not valid JSON: {exc}")
        sys.exit(1)

    repos = data.get("repos")
    if not isinstance(repos, list) or not repos:
        print("FAIL: config.json must have a non-empty 'repos' array")
        sys.exit(1)

    for i, entry in enumerate(repos):
        missing = [k for k in REQUIRED if not entry.get(k)]
        if missing:
            print(f"FAIL: repos[{i}] ({entry.get('name', '?')}) missing {missing}")
            sys.exit(1)
    return repos


def resolve_branch(entry):
    """Return (sha, detail). sha is None if the branch does not exist.

    Uses the git-refs endpoint, which genuinely 404s on a missing branch.
    /branches/{branch} is deliberately not used: it 301s to the default branch,
    so it cannot distinguish 'exists' from 'was redirected'.
    """
    status, body = _get(API.format(**entry))
    if status == 200:
        try:
            return json.loads(body)["object"]["sha"], "resolved"
        except (json.JSONDecodeError, KeyError) as exc:
            return None, f"could not read ref payload: {exc}"
    if status == 404:
        # The ref endpoint 404s for a missing branch AND a missing repo, so do
        # not blame the branch specifically.
        return None, f"no such repo/branch ({entry['owner']}/{entry['repo']}@{entry['branch']})"
    return None, body


def check(entry):
    """Return (ok, detail): does the branch exist and hold a parseable log?

    The log is fetched by commit SHA, not by branch name, so the content check
    cannot pass on a stale ref.
    """
    sha, detail = resolve_branch(entry)
    if sha is None:
        return False, detail

    url = RAW.format(owner=entry["owner"], repo=entry["repo"], sha=sha)
    status, body = _get(url)
    if status != 200:
        return False, f"log not readable at {sha[:10]}: {body}"

    lines = [ln for ln in body.splitlines() if ln.strip()]
    if not lines:
        return False, "status_log.jsonl is empty"
    try:
        json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        return False, f"last line is not valid JSON: {exc}"
    return True, f"{len(lines)} entries @ {sha[:10]}"


def check_board(path):
    """Validate the committed work_board.json: shape, and the keys the page reads.

    No network: this is a file in this repo, committed by the sweep. CI runs on
    the pull request that carries it, so a malformed or truncated board is
    caught before the page renders "could not be read" in production.

    Only the keys the page dereferences without a guard are required. The page
    reads `board.queue || {}` and `board.needs_you || []`, so those are optional;
    it reads `board.runs_by_day[...]` and `board.recent_runs` directly, so those
    must be present and of the right type.
    """
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return False, f"{path.name} does not exist"
    except json.JSONDecodeError as exc:
        return False, f"{path.name} is not valid JSON: {exc}"

    if not isinstance(data, dict):
        return False, f"{path.name} is not a JSON object"
    if not data.get("generated"):
        return False, "no 'generated' timestamp"

    by_day = data.get("runs_by_day")
    if not isinstance(by_day, dict):
        return False, "'runs_by_day' is not an object"
    for repo, series in by_day.items():
        if not isinstance(series, dict) or not series:
            return False, f"'runs_by_day[{repo}]' is not a non-empty day series"
        if any(not isinstance(v, int) or isinstance(v, bool) for v in series.values()):
            return False, f"'runs_by_day[{repo}]' has a non-integer day count"

    runs = data.get("recent_runs")
    if not isinstance(runs, list):
        return False, "'recent_runs' is not an array"
    for r in runs:
        if not isinstance(r, dict) or not r.get("run_id") or not r.get("repo"):
            return False, "a recent_runs row is missing run_id/repo"
        mins = r.get("minutes")
        # A measured duration is a non-negative number; an unmeasured one is
        # null. A zero is legitimate here (a run can finish in under a minute),
        # so the rule is duration/outcome agreement, not "reject zero".
        if mins is not None and (not isinstance(mins, (int, float))
                                 or isinstance(mins, bool) or mins < 0):
            return False, f"recent_runs[{r['run_id']}] has a non-numeric duration"
        if (mins is None) != (r.get("outcome") is None):
            return False, (f"recent_runs[{r['run_id']}] duration and outcome disagree "
                           f"(minutes={mins!r}, outcome={r.get('outcome')!r})")

    return True, f"{len(by_day)} repo series, {len(runs)} recent runs"


def board_self_test():
    """Prove check_board fails on broken input. Returns a failure count.

    Offline by design: each case writes a temp board, so the check is exercised
    without the network. Every case is a mutation a real bug could produce — a
    truncated write, a lost key, a zero smuggled in for an unmeasured duration.
    """
    import tempfile

    good = {
        "generated": "2026-10-19T11:44:07Z",
        "runs_by_day": {"ephapse": {"2026-10-18": 2, "2026-10-19": 1}},
        "recent_runs": [
            {"run_id": "20261019-1010-abcd", "repo": "ephapse",
             "issue": 39, "minutes": 12.5, "outcome": "done"},
            {"run_id": "20261019-0900-efgh", "repo": "Maith",
             "issue": 12, "minutes": None, "outcome": None},
        ],
    }

    def mutate(fn):
        import copy
        d = copy.deepcopy(good)
        fn(d)
        return d

    cases = [
        ("well-formed board", good, True),
        ("not an object", [1, 2, 3], False),
        ("no generated timestamp", mutate(lambda d: d.pop("generated")), False),
        ("runs_by_day not an object", mutate(lambda d: d.update(runs_by_day=[])), False),
        ("empty day series", mutate(lambda d: d["runs_by_day"].update(ephapse={})), False),
        ("non-integer day count", mutate(lambda d: d["runs_by_day"]["ephapse"].update({"2026-10-18": "2"})), False),
        ("recent_runs not an array", mutate(lambda d: d.update(recent_runs={})), False),
        ("run row missing repo", mutate(lambda d: d["recent_runs"][0].pop("repo")), False),
        ("unmeasured duration smuggled as 0", mutate(lambda d: d["recent_runs"][1].update(minutes=0)), False),
        ("measured run with a null duration", mutate(lambda d: d["recent_runs"][0].update(minutes=None)), False),
        ("negative duration", mutate(lambda d: d["recent_runs"][0].update(minutes=-1)), False),
        ("non-numeric duration", mutate(lambda d: d["recent_runs"][0].update(minutes="12")), False),
        ("legitimate zero-minute duration passes", mutate(lambda d: d["recent_runs"][0].update(minutes=0)), True),
    ]

    failures = 0
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "work_board.json"
        for label, data, must_pass in cases:
            path.write_text(json.dumps(data))
            ok, detail = check_board(path)
            mark = "ok  " if ok == must_pass else "BAD "
            print(f"  [{mark}] board: {label:44} -> {'pass' if ok else 'FAIL'} ({detail})")
            if ok != must_pass:
                failures += 1

    # A board that is missing entirely must fail, not crash.
    ok, _ = check_board(Path("/nonexistent/work_board.json"))
    mark = "ok  " if ok is False else "BAD "
    print(f"  [{mark}] board: missing file{' ' * 31} -> {'pass' if ok else 'FAIL'}")
    if ok is not False:
        failures += 1
    return failures


# --- self-test -------------------------------------------------------------
# Per this repo's own rule, a check is not trusted until it can be shown to
# fail on deliberately broken input. The regression this guards is specific:
# before the git-refs resolution, a *nonexistent* branch passed validation,
# because raw.githubusercontent.com serves a stale branch's last content with
# a 200. `ephapse` genuinely has no `dev` branch (GET /git/ref/heads/dev is
# 404) while raw.githubusercontent.com/philipdallen/ephapse/dev/... returns
# 200. If that case ever passes again, the bug is back.


def self_test():
    """Assert the checker fails on deliberately broken input. Returns exit code."""
    cases = [
        # (label, config entry, must_pass)
        (
            "known-good real branch",
            # Tracks live state: Maith's log moved to `status` when #63 landed.
            {"name": "Maith", "owner": "philipdallen", "repo": "Maith", "branch": "status"},
            True,
        ),
        (
            "phantom branch served 200 by raw",
            {"name": "Ephapse", "owner": "philipdallen", "repo": "ephapse", "branch": "dev"},
            False,
        ),
        (
            "never-existed branch",
            {"name": "Maith", "owner": "philipdallen", "repo": "Maith", "branch": "nope-xyz"},
            False,
        ),
        (
            "missing repo",
            {"name": "Ghost", "owner": "philipdallen", "repo": "no-such-repo-xyz", "branch": "main"},
            False,
        ),
        (
            "never-existed owner",
            {"name": "Nope", "owner": "no-such-owner-xyz", "repo": "Maith", "branch": "main"},
            False,
        ),
        # Must keep PASSING: the allenpd728 rename is a legitimate redirect, and
        # older cached links depend on it resolving. The fix must not break it.
        (
            "renamed owner (legitimate redirect)",
            {"name": "Old", "owner": "allenpd728", "repo": "Maith", "branch": "status"},
            True,
        ),
    ]
    failures = []
    for label, entry, must_pass in cases:
        ok, detail = check(entry)
        verdict = "pass" if ok else "FAIL"
        expected = "pass" if must_pass else "FAIL"
        mark = "ok  " if ok == must_pass else "BAD "
        print(f"  [{mark}] {label:38} -> {verdict} ({detail})  expected {expected}")
        if ok != must_pass:
            failures.append(label)

    if failures:
        print(f"\nSELF-TEST FAILED: {len(failures)} case(s) did not behave as required: {failures}")
        return 1

    board_failures = board_self_test()
    if board_failures:
        print(f"\nSELF-TEST FAILED: {board_failures} board case(s) did not behave as required")
        return 1
    print(f"\nSELF-TEST OK: {len(cases)}/{len(cases)} cases behave as required "
          f"(+ board checks)")
    return 0


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())

    repos = load_config(CONFIG)
    failed = 0
    for entry in repos:
        ok, detail = check(entry)
        status = "ok  " if ok else "FAIL"
        target = f"{entry['owner']}/{entry['repo']}@{entry['branch']}"
        print(f"  [{status}] {entry['name']:8} {target:28} {detail}")
        if not ok:
            failed += 1

    print(f"\n{len(repos) - failed}/{len(repos)} tracked repos resolve")
    if failed:
        print("FAIL: at least one tracked repo does not resolve")
        sys.exit(1)

    # The board is a core data file the page fetches; validate it too.
    ok, detail = check_board(BOARD)
    status = "ok  " if ok else "FAIL"
    print(f"  [{status}] work_board.json              {detail}")
    if not ok:
        print("FAIL: work_board.json is malformed")
        sys.exit(1)


if __name__ == "__main__":
    main()
