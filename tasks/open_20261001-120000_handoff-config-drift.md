# Task — Fix `HUB_AGENT_HANDOFF.md` branch table to match `config.json`

**Repo:** philipdallen/HuB
**Filed:** 2026-10-01T12:00:00Z by openhands `run=20261001-1200-rlm1`
**Directive:** DEC-001 (`docs/decisions/LOG.md`)
**Labels:** status:available, kind:repair, documentation

## Finding

`HUB_AGENT_HANDOFF.md` opens with a state-correction table that says ephapse is read from `dev` and Maith from `dev`, but `config.json` — which `AGENTS.md` declares authoritative ("config.json wins over any doc") — reads ephapse from `main` and Maith from `status`. The handoff therefore contradicts the file it describes, which is the same failure the handoff was written to prevent.

## Evidence (verified against raw files)

- `HuB/HUB_AGENT_HANDOFF.md` table: `ephapse` / `dev`, `Maith` / `dev`.
- `HuB/config.json`: `ephapse` / `main`, `Maith` / `status`.
- `HuB/AGENTS.md:25` — "config.json wins over any doc".
- `git log -- config.json` shows later fixes (`read Maith's status log from the status branch`, `read ephapse from main`) that the handoff was not updated for.

## Fix

Update the handoff's branch column (and its surrounding prose) to match the current `config.json`, or replace the hard-coded table with a pointer to `config.json` so it cannot drift again. Prefer the pointer.

## Verification

No branch value in `HUB_AGENT_HANDOFF.md` disagrees with `config.json`.

## Out of scope / do not re-derive

Do not change `config.json`. The current branch values are correct and verified (each `status_log.jsonl` resolves on the branch named).

---
Filed from the RLM Analyzer triage pass. Filing policy: DEC-001 in `docs/decisions/LOG.md`; consolidated record in `philipdallen/portfolio-ops` (`RLM_TRIAGE_2026-10-01.md`, branch `tasks/rlm-triage-2026-10-01`).

Directive: DEC-001
