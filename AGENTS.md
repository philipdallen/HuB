# AGENTS.md — HuB

Repository-specific context for agents working in this repo.

## What this repo is

A static GitHub Pages dashboard — **the work board**. `index.html` and
`config.json` at the repo root; no build step. It fetches `status_log.jsonl` from
each tracked repo over `raw.githubusercontent.com`, plus `work_board.json` from
this repo, and renders one tab per entry. It leads with run activity and the open
queue; every number links to the records behind it ("no orphan numbers").

Two data files, and the split is load-bearing:

- `status_log.jsonl` — append-only history. Fetched whole by the browser, so each
  line must stay small. The sweep appends one small `automation` summary per
  snapshot and skips the append when the summary is unchanged.
- `work_board.json` — current state (queue, recent runs, runs/day, needs-you).
  Rewritten in place each sweep, never appended, so it cannot grow. The detail
  lives here precisely so the append-only file stays small: a single line
  carrying the full board measured ~12 KB, which at ~5 appends/day is ~22 MB/year
  downloaded by every visitor.

## Hard constraints (do not break these)

- **The page cannot call the GitHub API.** It has no backend and no credentials,
  and the unauthenticated budget is 60 requests/hour *per visitor IP* — one page
  load that fetched issue comments would spend about a third of it and break the
  page for everyone behind a shared address. Anything the dashboard displays must
  be precomputed into `status_log.jsonl` / `work_board.json` by a scheduled job.
- **Only public repos can be tracked.** `raw.githubusercontent.com` serves
  private repos as 404 to an unauthenticated fetch. Do not add a private repo to
  `config.json`.
- **An absent field is a true statement; a zero is often a false one.** This is
  the rule the sweeps follow and the page follows. Render an unmeasured field as
  an em dash (`—`), never as `0`. A whole unreadable repo is omitted from the
  queue rather than shown as an empty queue. See `renderStrip`/`renderRecentRuns`
  in `index.html` and `portfolio-ops/METRIC_CONTRACT.md` (private) rule 4.
- **`config.json` wins over any doc.** README tables are snapshots; read the live
  value from `config.json`. The branch field is whatever branch the source repo's
  sweep writes to, which is not necessarily its default branch.
- **Escape every fetched value.** All log content is untrusted — issue titles and
  labels included. Pass it through `esc()` on the way into a template. A field
  added without `esc()` is an XSS hole; see `SECURITY.md`.
- **Board repo names are lowercase GitHub names; config names are display
  names.** The sweep keys `work_board.json` by GitHub repo name (`ephapse`),
  while `config.json` uses display names (`Ephapse`). Match case-insensitively on
  either field (`repoByName` in `index.html`) or links silently drop to plain
  text.

## Conventions

- Every check must be shown to fail. `tools/validate_config.py --self-test` and
  `automation_sweep.py --selftest` both run in CI. A check that cannot fail is
  not a check. When adding one, verify it by mutation: break the logic, confirm
  the selftest reports FAIL, then restore.
- Sweeps append exactly one JSONL line, skip the append when the computed
  snapshot is unchanged (unless `--force`), and never rewrite existing lines.
  `work_board.json` is the exception: it is rewritten every sweep by design.
- The queue counts open issues only (`state=open`), so a closed issue still
  carrying a stale `status:` label is excluded by construction.
- **A count must link to the records it was computed from, not a lookalike.**
  A claim count is read from the `claims/` files, so it links to `claims/`; the
  `status:claimed` label list can disagree (a repo may hold a lock without the
  label), so it is not the right drill-down. A run count links to the repo's
  issues, where the run comments live. A per-repo `flow` count links to the
  issue list for its own label. The `runs_24h` total is deduped by run-id while
  the per-repo breakdown is not, so their sum can exceed the total — say so in
  visible text, not only a tooltip.
- Workflows that push rebase-and-retry on rejection; never force-push, which
  would destroy a sibling's committed work.

## Environment note

`gh` reads `GH_TOKEN` before `GITHUB_TOKEN`, so a stale `GH_TOKEN` makes every
`gh` command fail `Bad credentials` while `GITHUB_TOKEN` is perfectly good: reads
and `git push` work, `gh` does not. That looks like an expired token and is not.

Do not `unset` it - **pin it to the live token**, which is correct whether the
incoming `GH_TOKEN` is stale or absent:

```bash
export GH_TOKEN=$GITHUB_TOKEN && gh api user -q .login
```

`GITHUB_TOKEN` is short-lived and has **no agent-side refresh step**: the platform
re-injects the current value into each command whose text contains the literal
string `GITHUB_TOKEN`. Reference it again in a new command to get a fresh value.
A long-running process captures the token at start and 401s after a rotation until
restarted; `git push` with a token embedded in the remote URL is the same trap
(use `GIT_TERMINAL_PROMPT=0` and re-point the remote).

Diagnose by testing each variable independently:

```bash
env -u GH_TOKEN bash -c 'curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com/user'
env -u GITHUB_TOKEN bash -c 'curl -s -o /dev/null -w "%{http_code}\n" \
  -H "Authorization: Bearer $GH_TOKEN" https://api.github.com/user'
```

Reads of public repos work fully unauthenticated, so `automation_sweep.py
--no-token` and `validate_config.py` run without any token.

Full mechanism and evidence: `portfolio-ops/ACCESS_AND_IDENTITIES.md`.
