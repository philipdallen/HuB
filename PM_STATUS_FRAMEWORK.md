# PM Status Framework — `status_log.jsonl` schema

> This document defines the record schema HuB renders. `HUB_AGENT_HANDOFF.md`
> references this file as living "in Maith, or copied here for reference"; it was
> not present in any tracked repo when HuB was created, so this copy is derived
> from what `index.html` actually consumes and is now the canonical reference.
> If a source repo maintains its own copy, keep the field names in sync with this
> one — `index.html` reads the fields below by name.

> **Rework note (2026-10-19).** HuB is now *the work board*: it leads with run
> activity and the open queue, and every number on the page drills into the
> records behind it. The `trl` block and the derived flow judgments
> (`blocked_ratio` colouring, `cycle_time_median_hours` as a headline) are no
> longer rendered. The `trl` schema is retained below because source repos still
> commit `status/trl.json` and their own READMEs render it; HuB simply does not.
> See `HUB_AGENT_HANDOFF.md` §"The work board".

## Purpose

Each tracked repo owns a `status_log.jsonl` at its repo root, on the branch named
in HuB's `config.json`. A per-repo **sweep extension** appends one snapshot object
per line, forever (append-only). HuB never writes this file; it only fetches and
displays it. See `HUB_AGENT_HANDOFF.md` §"What this repo is not".

Two files feed the dashboard:

- **`status_log.jsonl`** — append-only history, one line per snapshot. Fetched
  whole by the browser, so each line must stay small. HuB renders the last 12
  `notes` from it as the Snapshot log and reads the newest line's `automation`
  block for the header strip's run count.
- **`work_board.json`** — the current state: queue counts, recent runs, per-day
  run series, the "needs you" list. Rewritten in place each sweep (never
  appended), so it cannot grow without bound. Written by `automation_sweep.py`
  into HuB itself. See `HUB_AGENT_HANDOFF.md` §"The work board" for the shape.

Why the split: a single append-only line carrying the full work board measured
~12 KB against the old ~370 B line. At ~5 appends/day that is ~22 MB/year of
append-only history that **every visitor downloads on every page load**. The
static-page budget (no backend, public CDN) cannot absorb that, so the
accumulating file stays tiny and the current-state detail lives in a file that
does not accumulate.

## Record format

One JSON object per line (JSONL). Blank lines are ignored; a line that fails to
parse is silently dropped by the dashboard. Records are sorted client-side by
`timestamp`, so file order does not strictly matter — but append-only ordering is
the convention and is what keeps diffs readable.

```json
{"timestamp":"2026-09-19T19:00:00Z","trl":{"IR pipeline":4,"gates":2,"axiom search":1},"flow":{"wip":5,"blocked":1,"open_total":23,"blocked_ratio":0.043,"cycle_time_median_hours":18.5,"stale_reversions_since_last":0},"notes":"sweep: reclaimed 2 stale claims"}
```

### Fields

| Field | Type | Required | Meaning |
|---|---|---|---|
| `timestamp` | string (ISO-8601, parseable by `new Date()`) | recommended | When the snapshot was taken. Used for ordering and the Snapshot log. Missing/garbage timestamps collapse the ordering; always emit one. |
| `automation` | object (see below) | optional | Automation-health summary, written only by HuB's own `automation_sweep.py`. Drives the work board's header strip. |
| `trl` | object: `{componentName: integer 0–9}` | optional | Technology-Readiness-Level per component. **No longer rendered by HuB** (see the rework note). Source repos still commit it for their own READMEs. |
| `flow` | object (see below) | optional | Kanban/flow metrics from the latest sweep. The work board's queue section supersedes its headline judgments. |
| `notes` | string | optional | Free-text note. Records that have `notes` populate the "Snapshot log" (last 12 shown, newest first). |

### `automation` object

Written by HuB's own `automation_sweep.py`. Deliberately small: the log is
append-only and fetched whole, so the detailed current-state blocks live in
`work_board.json` instead.

| Key | Type | Meaning |
|---|---|---|
| `runs_24h`, `runs_7d` | number | Distinct run-ids whose encoded start falls in the window. The same id seen in two repos counts once. |
| `repos_active_24h` | array of string | Repos with at least one run in the last 24h. Omitted when empty. |
| `claims_total` | number | Claim files currently held. |
| `claims_held` | object `{repo: n}` | Per-repo claim-file counts. Omitted when no repo has the mechanism, since a repo without `claims/` would contribute a false zero. |
| `stale_claims`, `stale_claims_total` | array, number | Claims whose issue is already closed. Omitted / zero when none. |
| `last_run`, `last_run_id` | string | The most recent run's start and id. Omitted when no runs are visible. |
| `available_total` | number | Open `status:available` issues across tracked repos. Lets a falling run count be told apart from an empty queue. |

**Deliberately absent: failure rate and idle rate.** A failed run dies before it
comments, so it leaves no public trace. An absent field renders as an em dash,
never as `0`.

### `flow` object

**Definitions are owned by `portfolio-ops/METRIC_CONTRACT.md` (private).** That
document wins: if this table and the contract disagree, this table is the bug.
It is private because it records the change history and the reasons behind
definition changes; the definitions themselves are reproduced here so the
schema stays readable.

| Key | Type | Meaning |
|---|---|---|
| `open_total` | number | All open issues (PRs excluded). Denominator for `blocked_ratio`. |
| `wip` | number | Issues labelled `status:claimed`. |
| `available` | number | Issues labelled `status:available`. |
| `blocked` | number | Issues awaiting **human input** — `status:blocked-needs-input`, excluding `on-hold` and `auditor:*` (the audit queue, tracked separately). **Redefined at contract v1 2026-09-21.** |
| `janitorial` | number | The excluded `status:blocked-needs-input` count (`on-hold` / `auditor:*`). Present only from contract v1, so its presence marks a v1 record. |
| `needs_review` | number | Issues carrying `needs-review` or any `review:*` label. |
| `blocked_ratio` | number 0–1 | `blocked / open_total`. **HuB no longer colours this** (a derived judgment with no drill-down); the raw `blocked` count is what the queue shows. |
| `cycle_time_median_hours` | number | Median cycle time, hours. Omitted when nothing closed in the 30-day window. **No longer a headline on HuB.** |
| `closed_last_30d` | number | Closed issues in the same window. Present only with `cycle_time_median_hours`. |
| `stale_reversions_since_last` | number | Reversions discovered by the stale-claim sweep since the previous snapshot. |

An absent `flow` key renders as an em dash, **not** `0`. A zero published for a
field that was never measured is a false statement, which is `hub_sweep.py`'s own
rule. This is also the work board's rule: the queue shows an em dash for a repo
whose issues could not be read, never a zero.

**Why `blocked` is scoped.** Before v1 it counted every `status:blocked-needs-input`
issue regardless of cause. On 2026-09-21 the auditor relabelled five of its own
`on-hold` count-drift issues to that status, and ephapse's published
`blocked_ratio` jumped from `0.071` to `0.415` — rendered as crit — without
anything about the program changing. A metric a janitorial relabel can move is
not a program metric. History is append-only, so the correction appears as a
visible discontinuity in the trend, never as an edited past.

### TRL rendering bands

Source repos colour a TRL bar by value in their own READMEs: `>= 6` teal,
`>= 4` blue, `>= 2` amber, otherwise rose. These thresholds are presentation
only — they are a glanceable cue, not a second definition. HuB does not render
TRL at all (see the rework note); the meaning of a level is the table below.

## What TRL means here

TRL (Technology Readiness Level) is a 1–9 scale for how far a capability is from
a written idea to routine operational use. Applied to a research repo, the
"technology" is a **component**: a named capability the project intends to rely
on or hand to someone else — not the project as a whole, and not its research
hypothesis.

### The scale is derived from the ladders these repos already run

Rather than importing NASA's hardware scale, the levels below are the **join of
the maturity structures already in use** in the four tracked repos. Each was
already precise, already battle-tested, and already enforced by tooling:

| Existing structure | Where it lives |
|---|---|
| **Typed → Validated → Frozen** (a definition's status ladder; "anchor" forbidden below frozen) | PleaNP `docs/VALIDATION_SUITE.md` §"Status ladder" |
| **Observed → Recorded → Surface-controlled → Causal per-feature → Causal aggregate → Handed off** (applied to claims; explicitly notes which rungs are not currently reachable) | ephapse `docs/reference/TEST_VALIDATION_SPEC.md` §5 |
| **"A check must be able to fail"** — every gate ships a fixture that makes it fail; a check that cannot fail is not a check | ephapse `TEST_VALIDATION_SPEC.md` §4; PleaNP gate-scanner self-tests; Maith CI |
| **Five stations, five gates** (`IR Build → Corpus → Dataset → Training → Evaluation`, each gated) | Maith `docs/reference/PIPELINE_QUALITY_GATES.md` |
| **Phase "done when" criteria** + own-vs-product split (deterministic player is the free baseline; the LLM player is the product) | rubato `docs/pipeline.md` |

The mapping is not decorative — it is what makes a level defensible. A level is
only claimable if the corresponding structure in that repo says so.

| Level | Name | Cross-repo meaning | Nearest existing structure |
|---|---|---|---|
| 1 | Specified | Idea and scope written down. No code. | A Maith hypothesis entry; a rubato design doc; an ephapse filed issue |
| 2 | Scaffolded | Files exist; may be stubs, placeholders, or `sorry`'d bodies. Compiles at best. | PleaNP **Typed** ("parameters may be unused; bodies may be sorry'd") |
| 3 | Runs on real inputs | Works on actual project inputs, not only a synthetic or toy case. | ephapse **Observed**; pre-Validated PleaNP |
| 4 | Mechanically gated | Passes the repo's own Tier-0/CI scanners (hygiene, vacuity, model-consistency, label hygiene, conformance). | ephapse **Recorded**; Maith Tier-1 gates; rubato task `done` |
| 5 | Ladder-validated | The repo's own validation suite passes — must-prove *and* must-refute proven, no `sorry`, every parameter load-bearing, and each check has a fixture proving it can fail. | PleaNP **Validated**; ephapse's check-must-fail rule |
| 6 | Proven at intended scale | Ran end-to-end on the full intended corpus/input set, not a subset. | ephapse **Causal aggregate** (its strongest currently attainable rung); rubato phase "done when" |
| 7 | Consumed elsewhere | Something outside the component's own tests depends on it: a sibling module, another repo, or a human using its output for its real purpose. | ephapse **Handed off** (a human passes a candidate to Maith); Maith importing PleaNP; PleaNP's root lakefile existing so downstream repos can `require PleaNP` |
| 8 | Frozen / anchor | Gates passed **and** human review done. It is the canonical reference; changes are deliberate revisions, not churn. | PleaNP **Frozen** ("proof search may run against it") |
| 9 | Operational | Sustained real use producing results, with routine maintenance rather than active development. | — (no repo claims this yet; that is expected, not a gap) |

**Level 7 before 8 is deliberate.** A capability can be depended on by a consumer
(e.g. a hand-off to a sibling repo, or a module others import) while still not
being frozen as a canonical anchor. PleaNP's ladder orders it the same way in
practice: proof search may only run against a *Frozen* definition, which is a
stricter bar than merely being used.

**A level above a repo's own ceiling is not available.** ephapse records that its
rung 3 is "not currently reachable at pythia-70m" and that rung 5 "is not
reachable by an agent". Where a repo documents a ceiling, a component cannot be
rated above it however good the tooling looks.

### Rules that keep the number honest

These are the same disciplines the repos already enforce on their own ladders,
restated for a cross-repo number:

- **Rate the weakest real capability, not the best demo.** A component is only as
  ready as its least-ready load-bearing part.
- **A component can go down.** If a regression invalidates a claimed property,
  lower the number. A monotonic TRL is a warning sign, not a goal. This is
  PleaNP's reset notice and ephapse's rung-3 findings applied to a single field.
- **A check that cannot fail is not a check — and does not raise readiness.**
  Passing a scanner that would also pass on a broken input is not evidence. This
  is ephapse `TEST_VALIDATION_SPEC.md` §4 (`RUNS-MUST-DETECT`, the positive-control
  rule) and PleaNP's gate-scanner self-tests.
- **TRL is about the component's readiness, not the project's confidence in its
  hypothesis.** A well-built tool for testing a hypothesis that turned out false
  can still be rated 8. Ephapse states this explicitly: its rungs are about the
  apparatus, and a null result does not lower the instrument.
- **Do not claim a level the repo's own ladder cannot reach.** ephapse documents
  that its rung 3 is not reachable at pythia-70m; a component there cannot be
  rated as though it were.
- **It is a judgement, not a measurement.** Do not invent decimals or derive it
  from issue counts — that is exactly why the sweep refuses to compute it.
- **Prefer several honest components over one vague aggregate.** `"project": 5` is
  not a useful entry.
- **Not every component needs a level.** Omit anything you would not defend.
  `null` in the template means "not rated", which is a truthful state — and the
  sweep skips it rather than reading it as 0.

### Worked example: how to arrive at a number

Applying the table to a real component, so the reasoning is checkable rather than
declared. PleaNP's **Barrier Calculus** (Rung 5; `lean/PleaNP/Calculus/`, ~1,049
lines across seven modules; covered by CI and by a `#barrier_check` verdict
harness that fails if an expected verdict line is missing or flips):

| Level | Met? | Evidence checked |
|---|---|---|
| 2 — Scaffolded | yes | Modules exist and are built by CI. |
| 4 — Mechanically gated | yes | CI runs hygiene, vacuity, model-consistency, unicode, and binder-usage scans over `lean/PleaNP/Calculus`, plus `barrier_check_test.py`, which asserts the four `#barrier_check` verdict lines and fails on a regression. |
| 5 — Ladder-validated | **no** | PleaNP's *Validated* bar (its own words) requires "must-prove lemmas are proven (not sorry'd), must-refute lemmas are proven, smoke tests pass. Every parameter is load-bearing." The binder scan returns 17 REVIEW items (advisory dead-code candidates, not violations), and the component has no entry in `VALIDATION_SUITE.md`. |
| 8 — Frozen | **no** | PleaNP's own rung table still reads "In progress (prototype in `lean/PleaNP/Calculus/`)", and its machine-readable `formalization.yaml` vocabulary only ever reaches `rendered-not-frozen` / `validated-in-ci` — never `frozen` for this work. |

So Barrier Calculus is **4**. Note what did *not* decide it: the module is large,
ambitious, and CI-green, and a careless reading would call that "nearly done".
The number came from checking the repo's own stated bar and finding one specific
condition unmet — and that condition is quotable, so a reader who disagrees can
point at it rather than argue about adjectives.

The method also works in the other direction. A component that is `done` on rubato's
Phase table **and** covered by its conformance runner legitimately reaches 4 even
while development continues, because "gated" and "finished" are different claims.
And ephapse's validation layer, which the README calls a first-class output with
`RUNS-MUST-DETECT` discipline, can reach 4 on the strength of its own gate runner
(11/11 gates pass) without any claim about the scientific finding it serves.

### These levels are a summary, not a second tracker

Where a repo's own doc and HuB disagree, **the repo's own doc wins** — the same
rule that already applies to `flow`. Do not maintain two sources of truth:

- Where a repo's own doc and HuB disagree, **the repo's own doc wins**.
- Name TRL components after **capabilities you would hand to someone else**, not
  after internal tasks. A rung table says *what is in scope and in what order*; a
  TRL level says *how usable that piece is right now*. That keeps the axes
  genuinely different.
- Update a level when the piece's **reusability** changes, not when a task moves
  label. If a task transition would change nothing for a consumer, it should not
  change the number.
- Do not restate per-item statuses in TRL. If you find yourself tracking more than
  roughly five components, you have started building the second tracker this
  section exists to prevent — point at the repo's doc instead.

### Who updates it, and when

TRL is set by a human who knows the project — typically the maintainer — and
reviewed at the same cadence as the repo's own status doc. It is **not** an agent
task and should not be changed by an automated sweep; the sweep only copies the
committed values into the next snapshot.

Mechanically: commit `status/trl.json` at the repo root on the tracked branch.
A change appears in that repo's own README on the next sweep (nominally every 30 minutes, but GitHub throttles scheduled runs: observed ~7 runs/24h, median gap 2.3-2.7h, worst 6.3h; plus the
CDN's ~5 minutes). Each source repo's `docs/HUB_STATUS_LOG.md` lists that repo's
candidate components and carries a `status/trl.json.template` to start from —
rename the template to `trl.json` once real levels are set. Until then the field
is simply absent, which is the correct state for a repo that has not made the
judgement yet.

## Where the data comes from

The sweep extension in each **source** repo is the writer. This is the only
sanctioned write path. HuB is a read-only mirror; if HuB and a source repo's own
tracker disagree, the source repo wins (HuB can also lag ~5 minutes behind a
commit because `raw.githubusercontent.com` is CDN-cached).

For the multi-agent workflow that produces these snapshots — run-ids, atomic
label claims, and stale-claim sweeps — see the source repos' own
`docs/MULTI_AGENT_WORKFLOW.md`.

## Current state

All four tracked repos (Maith, PleaNP, ephapse, rubato) have the sweep extension
and the scheduled `hub_sweep` workflow installed on their tracked and default
branches, so `status_log.jsonl` exists and is appended to automatically. Their
own READMEs render real `flow` data.

`trl` is **populated** as of 2026-09-20. Each repo's `status/trl.json` carries a
`components` map plus a `_rationale` map recording, per component, the exact rule
and evidence the level was read off — so a rating can be challenged on its
evidence rather than its adjective. `_rationale` is ignored by the sweep parser
by design; only `components` is read. HuB itself no longer renders `trl` (see the
rework note at the top); the source repos' own READMEs do.

Levels are **derived from each repo's own ladder**, never estimated. A component
is only rated where the repo's own structure says so, and where a repo documents
a ceiling the level stops there.

Raising a level means changing the underlying evidence first, then the number —
not the other way round. When a component's evidence changes, update
`status/trl.json` in that repo; the dashboard follows on the next sweep.