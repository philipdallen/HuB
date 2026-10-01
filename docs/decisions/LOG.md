# Decision log

Chronological record of decisions for HuB. Append-only. Format: `### DEC-0NN`
with Date, Status, Scope, Decision, Rationale.

---

### DEC-001

**Date:** 2026-10-01
**Status:** Active
**Scope:** Task protocol
**Decision:** Triage filing policy for the RLM Analyzer pass over HuB.

- **D1** — An RLM Analyzer report is unverified LLM triage. A finding becomes a
  task only after it is confirmed against raw files in this repo.
- **D2** — A refuted or stale finding gets no task; it is recorded in the triage
  summary with the evidence that refutes it.
- **D3** — Generic web-application security advice (authentication,
  authorization, API validation, security headers, WAF, pen testing,
  SAST/DAST, log anomaly detection) does not apply to a static dashboard that
  runs no server and accepts no input, so no task is filed for it.
- **D4** — One task per verified finding, no bundling and no extra scope.
- **D5** — Anything that needs a human choice is filed as a blocker for the
  owner, not decided by the agent.

**Rationale:** The reports are a triage aid, not a source of truth. Verification
against raw artifacts is the only step that separates a real defect from a
plausible-sounding one. `AGENTS.md` already states "config.json wins over any
doc", so a doc that contradicts it is a real defect; a security recommendation
for a server that does not exist is not.

**References:**
- `rlm-triage-summary.md` in `philipdallen/portfolio-ops`
  (branch `tasks/rlm-triage-2026-10-01`)
- `AGENTS.md` §Conventions, `HUB_AGENT_HANDOFF.md`
