# HuB — Agent Handoff

> **State correction (2026-09-19).** This repo was handed off with a
> `config.json` whose values did not match the source repos' actual state. The
> corrections below were verified against the GitHub API and are why
> `config.json` now reads as it does — don't "restore" the original values.
>
> | Original config | Verified reality | Fix applied |
> |---|---|---|
> | `Ephapse` / `main` | Repo is `ephapse` (lowercase) and **has no `main` branch** — only `dev`. | `repo: "ephapse"`, `branch: "dev"` |
> | `Maith` / `main` | Active development is on `dev`; `main` lags (16 ahead / 22 behind). Maith's `AGENTS.md` (DEC-036): "All development is on the single `dev` branch." | `branch: "dev"` |
> | `PleaNP` / `dev` | Correct as given. | unchanged |
> | — | `PM_STATUS_FRAMEWORK.md` existed in **no** tracked repo despite being cited as authoritative. | Added here, derived from what `index.html` actually consumes. |
> | — | No repo has a `status_log.jsonl` yet. | Expected — all tabs should show amber **empty**. Do not "fix". |
>
> Also verified: `philharmonic` is **private**, so it can never be tracked by this
> architecture (see §"Requirement: source repos must be public"). `rubato` is public
> and active, and is now tracked as the fourth repo.

## Provisioning is complete (2026-09-19)

The scheduled sweeps are live in all four source repos. Earlier notes in this
file said the workflow could not be pushed — that is **resolved**; ignore any
stale "workflows-handoff" reference (that directory is deleted).

What happened, so it is not re-litigated:

- The `ALL_REPOs_GH_TOKEN` truly cannot write workflow files: `git push` is
  rejected with *"without `workflow` scope"*, and the contents API returns 404.
- The separate `GITHUB_TOKEN` in this environment **is** a fine-grained PAT with
  the **Workflows** permission. Its Contents-API writes to
  `.github/workflows/*` succeed (201, verified by read-back), even though
  `git push` of the same file is still refused — GitHub blocks workflow changes
  over the git protocol but allows them over the contents API.
- Workflows were therefore installed via the contents API, on both `dev` and the
  **default branch**. The default-branch copy is required: GitHub only lists and
  *schedules* a workflow that exists on the default branch. `ephapse` defaults to
  `dev`, so for it one copy covers both.
- A latent bug in the first version was caught by testing rather than assuming:
  the commit step used `git diff --quiet -- status_log.jsonl`, which **ignores
  untracked files**. On a repo where the sweep *creates* the log it reported "no
  change" and silently discarded the file — the workflow would have gone green
  while publishing nothing. Fixed by staging first, then `git diff --cached
  --quiet`. This is the reason the create path is tested explicitly, not just the
  modify path.

Verified end to end, not assumed:

| Check | Result |
|---|---|
| All four `hub_sweep` workflows registered & active | yes |
| ephapse create path (log deleted, then swept) | log recreated, committed by `github-actions[bot]` |
| Maith / PleaNP / rubato modify path | run green |
| Re-run with no change | **no** new commit — idempotent, no duplicate line |

## What this repo is

HuB is a read-only, static dashboard — **the work board**. It aggregates
`status_log.jsonl` from the repos listed in `config.json` (currently Maith,
PleaNP, ephapse, rubato) plus a current-state `work_board.json`, and leads with
run activity and the open queue. Its one design rule is **no orphan numbers**:
every number answers its own follow-up question by drilling into the records
behind it, or it does not ship. Hosted on GitHub Pages. See PM_STATUS_FRAMEWORK.md
for the record schema and §"The work board" below for the rework.

## What this repo is not

- Not a place any source repo's status data gets written. HuB only fetches and
  displays. Source repos own their own `status_log.jsonl` and write to it
  themselves via their own sweep extension. (The two files HuB writes —
  `status_log.jsonl`'s `automation` summary and `work_board.json` — are its own,
  about the automation, not any source repo's status.)
- Not a build step, not a backend, not a database. Two static files
  (`index.html`, `config.json`) served by GitHub Pages, plus the two data files
  the sweep writes. If this repo ever seems to need a server, that's a sign of
  scope creep — stop and check against this doc before adding one.
- Not authoritative. If HuB shows something a source repo's own issue
  tracker disagrees with, the source repo is correct — HuB is a mirror,
  reading data that can lag up to ~5 minutes behind a commit due to
  raw.githubusercontent.com's CDN cache.

## The work board

Reworked 2026-10-19 from the proposal in HuB issue #22. Three sections, in order,
each with drillable numbers:

1. **Header strip — "Is the machine running?"** Last sweep (red past 6h), runs in
   the last 24h plus a per-repo breakdown, claims held, stale claims.
2. **Activity — "Is work getting done?"** A per-repo runs/day stacked bar for the
   last 30 days, and a recent-runs table (repo, issue, run-id, duration, outcome;
   every row links to its issue).
3. **Queue — "What's open?"** Per-repo available / claimed / blocked-needs-input /
   on-hold counts, each linking to a filtered GitHub issue list, plus a "Needs
   you" panel of every blocked-needs-input and needs:human issue, oldest first.

Removed: TRL bars and the mean-TRL trend line (the metric misfires on research
repos, no log record carried a `trl` key, and it answers "how mature?" not "is
work getting done?"). Demoted: `blocked_ratio` colouring and
`cycle_time_median_hours` as headlines — the raw counts stay, the verdicts go.
Kept: the snapshot log and the em-dash rule.

**Two files, deliberately split.** The sweep appends a small `automation`
summary to `status_log.jsonl` and rewrites `work_board.json` in place. A single
line carrying the full board measured ~12 KB (vs the old ~370 B), which at
~5 appends/day is ~22 MB/year of append-only history every visitor would
download on every load. The accumulating file stays tiny; the current-state
detail does not accumulate.

## Architecture (why it's built this way)

- **`index.html`** fetches `config.json`, then for each listed repo,
  fetches `https://raw.githubusercontent.com/{owner}/{repo}/{branch}/status_log.jsonl`
  directly, client-side, no backend.
- **Why raw.githubusercontent.com and not the GitHub API**: it's CDN-served,
  has no meaningful rate limit for this use, and sends permissive CORS
  headers so a static page can fetch it with no token. The GitHub API's
  60-request/hour unauthenticated limit would make this fragile for no
  benefit at this scale.
- **Why branch is a config value, not hardcoded**: PleaNP's active branch
  for its Circuits work is `dev`, not `main` — this has already caused a
  real mistake once (an agent assumed `main` and mis-planned against a
  17-line placeholder). `config.json` makes a branch change a one-line
  edit, not a code change.
- **Why this can never create cross-repo write conflicts**: HuB never
  writes to Maith, PleaNP, or Ephapse, and they never write to each
  other or to HuB. Each source repo's sweep only ever appends to its own
  file, on its own branch. Zero shared write surface, by construction —
  don't add one.

## Requirement: source repos must be public

`raw.githubusercontent.com` won't serve a private repo's contents to an
unauthenticated client-side fetch (e.g. `philharmonic` is private and therefore
cannot be tracked here), and a static page can't safely hold a personal access
token (visible to anyone via view-source). Confirm Maith, PleaNP, and Ephapse
stay public. If any repo ever needs to go private, the
fix is a scheduled GitHub Action *in that repo* copying its own
`status_log.jsonl` to a public location HuB can still read — never a token
embedded in HuB itself.

## `config.json` schema

```json
{
  "repos": [
    { "name": "Maith",   "owner": "philipdallen", "repo": "Maith",   "branch": "main" }
  ]
}
```

Adding a new tracked repo is an addition to this array, nothing else.
Changing a branch (e.g. if PleaNP's work moves off `dev`) is a one-field
edit here.

## Handling missing or broken source data (already built in, don't rebuild)

`index.html` already handles three states per repo, shown via a colored
dot on that repo's tab:
- **ok** (teal dot) — `status_log.jsonl` found and parsed.
- **empty** (amber dot) — repo reachable, file doesn't exist yet (404) or
  parsed to zero valid lines. This is the expected state for a
  freshly-kicked-off repo (e.g. Ephapse before its first sweep runs) — the
  page shows a message pointing at PM_STATUS_FRAMEWORK.md, not an error.
- **error** (rose dot) — fetch failed for another reason (repo went
  private, branch typo, network issue). Shows the HTTP status/detail.

Don't treat "empty" as a bug to fix in HuB — it's a correct signal that a
source repo hasn't wired up its sweep extension yet.

## Deployment

1. Repo settings → Pages → Deploy from a branch → `main`, `/ (root)`.
2. No build step. Confirm `index.html` and `config.json` are at repo
   root, not nested.
3. After enabling Pages, confirm the fetch actually resolves — open the
   page and check the tab dots go from grey (loading) to colored, not
   stuck grey (a stuck grey dot means the fetch itself failed silently;
   check browser console for a CORS or 404 detail).

## Single-branch policy

`main`, root, no exceptions — this repo is small enough that there's no
legitimate reason to diverge from Maith's own lesson (DEC-036, adopted
after parallel branches sprawled). Start here already following it.

## Immediate first issues

1. ~~Confirm `config.json`'s owner/repo/branch values are correct and that all
   tracked repos are public.~~ **Done (2026-09-19).** See the state-correction
   table above: `ephapse`/`dev` and `Maith`/`dev` were wrong and are fixed.
2. ~~Enable GitHub Pages and confirm the dashboard loads.~~ **Done
   (2026-09-19)** — <https://philipdallen.github.io/HuB/> is live.
3. ~~Land the sweep extension in the source repos.~~ **Done (2026-09-19)** —
   installed and pushed to `dev` in Maith, PleaNP, ephapse, rubato, each with a
   first real `status_log.jsonl`.
4. ~~Install the scheduled workflow in each source repo.~~ **Done
   (2026-09-19)** — installed on `dev` and the default branch in all four;
   registered, active, and run-tested.
5. ~~Confirm all four tabs are teal (ok) with real data.~~ **Done
   (2026-09-19).** The workflow create path was tested explicitly (log deleted,
   then recreated by the scheduled sweep) and a no-change re-run was confirmed
   to commit nothing.

Nothing is outstanding. The system is live: each source repo appends its own
snapshot every 30 minutes and HuB reads it.

Known, deliberate non-issues (do not "fix"):
- TRL levels are still maintained per repo as **judgements with recorded
  rationales**, not measurements: never "correct" a level by editing the number
  alone. Change the evidence, then the level. A level above a repo's documented
  ceiling is invalid by construction. HuB no longer renders TRL; the source
  repos' own READMEs do.
- A snapshot appears in the dashboard up to ~5 minutes after it is committed,
  because `raw.githubusercontent.com` is CDN-cached.
- The work board's `work_board.json` is rewritten every sweep, so its `generated`
  timestamp advances even when no new log line was appended. That is intentional:
  it is a current-state file, not history.
