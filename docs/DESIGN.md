# Controller design (working draft)

Written before coding. Numbers marked *(measure)* get replaced by measured values in SUBMISSION.md.

## 0. What the data and policies already told us

- **History has one variant only.** For `nw_renewal` all 141k history calls are W1/V1/P1, with caller IDs D1/D2
  split about 50/50. So history can bootstrap **call-time** and **caller-ID** knowledge. It says nothing about
  welcome message or voice. Those must be learned by experiment from D0.
- **Policies differ per workflow** (read from `GET /workflows`, never hard-coded):
  - `nw_renewal`: auto-apply for `schedule` and `caller_id` only, `max_shift` 0.5. 3 requests per 24h.
  - `nw_kyc`: no auto-apply. `mid_flight: next_campaign_only`. Primary KPI is pickup, guardrail is engagement.
    2 requests per 24h.
  - `ks_cancel`: auto-apply for four dims with `max_shift` 0.2. 4 requests per 24h. TTL is 12h.
- **Traffic is small.** `nw_renewal` gets about 3.6k dials/day and about 940 connected calls/day. A cohort that is
  1/3 of that gives about 300 connected calls/day. Fine-grained cohorts cannot reach significance in 8 days.
  This drives everything below.

## 1. Principles

1. Governance first. A change is either approved, or auto-applied where policy allows it. Nothing else.
2. Spend traffic on few, large cohorts. Split only when evidence says the effect differs.
3. An LLM only proposes prompt candidates. A deterministic test picks winners.
4. Voluntarily stricter than policy where a human is waiting on the result (customer-facing changes).
5. Every decision is written to our own store **before** the side effect, with the reason.

## 2. Architecture

```
 sim /advance ──► ingest ──► SQLite (WAL) ◄────────── audit / why CLI
 sim chunks  ──►  (tx: cursor+aggregates+audit rows)     ▲
                      │                                    │
                      ▼                                    │
   detectors ─► planner ─► proposal queue ─► governance client ─► sim /approvals, /apply, PUT config
   (pickup, engagement,   (experiments,        (state machine,
    caller-ID, halluc.)    winners, rollback)   quota/hours/TTL)
```

One process, one loop, one tick = one `/sim/advance` of 15 minutes. Per tick:
1. Reconcile approvals and apply anything approved (never let `apply_by` pass).
2. Ingest new chunks into the store.
3. Run detectors and planners per workflow.
4. Release queued proposals that fit hours and quota.

**State** is SQLite in WAL mode, accessed through one thin `store` module. It has zero infrastructure and
`./run.sh` works on a clean box. Postgres is the production target (the SRE memo prefers it) and the `store`
interface is the seam for swapping it in.

**Restart.** The chunk cursor is committed in the same transaction as the aggregates it produced, so ingestion
is exactly-once. Proposal state is written before every external call. On start we reconcile `GET /approvals`
against our own table. Proposal IDs are deterministic (a hash of workflow, base version and content), so a
retry after a crash gets `409 duplicate_proposal_id` and we adopt the existing proposal.

## 3. Data model (audit store)

`cursor`, `call_agg` (per workflow, cohort, variant, config version, day, hour: dials, connected, engaged,
halluc_flags), `call_audit` (compact row per call: lead, ts, version, cohort, variant, so *"what did customer X
hear and why was it live"* is answerable within the 24h compliance ask), `cohorts`, `experiments`, `arms`,
`decisions` (the why: inputs, thresholds, outcome), `proposals`, `proposal_log` (every transition),
`config_snapshots`, `incidents`, `credit_ledger`, `loop_metrics`.

## 4. Cohorts (one shared object)

A cohort is a row in `cohorts` with an ID and a `where` clause, and it is the same object config, detection,
experiments and the supervisor view all refer to. Start with **one cohort per workflow** (`default`). Split on
`lang` first, then `age_band`, only when a variant × segment interaction test passes at the experiment's alpha and
each side has at least N connected calls (*measure*, about 600 per arm). Cap at 4 cohorts per workflow, since
fragmentation costs speed.

## 5. Experiments

- **Design.** Allocations are sampled independently per dimension, so welcome × voice is an orthogonal factorial
  design for free. We analyse **marginal effects per dimension** (traffic goes to both at once) and run an interaction
  check on the final combination before promotion.
- **Allocation.** The champion keeps at least 40% during exploration, challengers share the rest. After a winner
  is promoted, a **10% holdout** stays on the previous champion. That is what makes decay detectable.
- **Winner test.** Two-proportion z-test on the primary KPI against the champion, Holm-corrected across
  challengers, with alpha spent over at most 5 looks. Promotion also needs a practical-lift floor (at least
  1.5pp) and a minimum sample (about 1,500 connected calls per arm for a +5pp lift at 80% power). Cost: slower
  decisions, fewer false winners.
- **Guardrail.** A winner is rejected if the one-sided CI on the guardrail KPI crosses `max_drop`. (The Kestrel
  "reads the script word for word" story is exactly engagement collapsing with a clean safety score.)
- **Only what ran counts.** Each call carries `config_version` and `variant`. Arms are credited only from
  calls dialled under a version in which they were live. Not-yet-approved arms contribute nothing.
- **Supervisor edit mid-experiment.** A `config_changed` event with `actor: supervisor` starts a new regime.
  Stats for the touched dimensions are reset, the experiment is re-planned from the new base, and we never revert
  their change.

## 6. Decay and rollback

Compare the promoted variant against the 10% holdout over a rolling window. A sustained shortfall (about 2 consecutive
windows, one-sided, relative to the expected lift) triggers rollback. Rollback is auto-applied where policy
allows it (it is a safety action and must not wait for an approver). Where not allowed (Northwind welcome/voice),
it goes through approval at top priority using a **reserved quota slot** (1 of the daily requests is never spent
on optimisation).

## 7. Call time and caller ID (from history)

- **Call time:** per cohort, hourly pickup rate from history, Beta-smoothed. Convert to `hour_weights` with a
  floor so no hour gets weight 0 and capacity isn't wasted. Applied via auto-apply for `nw_renewal`/`ks_cancel`,
  via approval for `nw_kyc`. Per-person `lead_preferred_hour` is **cut**.
- **Caller ID:** rolling per-ID pickup against sibling IDs. A significantly worse ID has its weight reduced but
  keeps about 5% so recovery is visible. Auto-apply where allowed.

## 8. Incidents (hallucination)

Detect: rolling hallucination-flag rate per workflow against its own baseline, one-sided test, with a minimum
number of connected calls (the detector is noisy, so we act on rates, not single flags). Respond: one
`suggest-fixes` call (3 credits), validate each candidate at n=200 (2 credits each), pick the best that passes
`min_validation_pass_rate` **and** is not a near-verbatim script (like P5), then propose with evidence. Verify after
apply: the flag rate must fall, and engagement must not drop. Otherwise roll back. Credits are budgeted per
workflow with a reserve so an incident never meets an empty wallet.

## 9. Governance client

State machine per proposal: `DRAFT → QUEUED → SUBMITTED → APPROVED → APPLIED`, with side exits
`REJECTED` (cool-off, then re-plan), `EXPIRED` (counts as a rejection, re-queue with fresh data and a new ID),
`LAPSED` (re-queue), `STALE` (409: rebase onto the current version and re-propose), `SUPERSEDED`.
Timing: submit only inside approver hours and when quota is available, batch all pending changes for a workflow
into one request, poll every tick, apply the tick an approval appears.

**Auto-apply policy of ours:** only for operational dims (`schedule`, `caller_id`) and for rollbacks. Everything
customer-facing (welcome, voice, prompt) goes through approval **even where auto-apply is permitted** (Kestrel),
because the client asked to know before anything changes and compliance wants a named approver.

**Impact score:** estimated lift (pp) × affected lead share, scaled to [0,1], with the formula stored in the
proposal. **Affected leads:** count of leads in the cohort(s) whose allocation changes over the next campaign
horizon. **Dry run:** replay of the proposed allocation against the historical/observed cohort data (expected
KPI, with interval). Dry runs are estimates and labelled as such.

## 10. Scope cut list

| Cut / partial | Why |
|---|---|
| C4 per-person call time | Only 20–40% of leads return, so per-person history is too thin. Cohort-level hours capture most of it. |
| C15 fairness across tenants | Quotas are per workflow already, so contention is limited. Revisit at 10×. |
| C3 bootstrap | Used for call time and caller ID only, since history has a single welcome/voice variant. |
| UI for C11 | JSON view + CLI. A page is cheap later. |

## 11. Build order

1. Store + ingestion + restart-safe cursor. (floor)
2. Governance client + audit `why` query. (floor)
3. Call-time and caller-ID bootstrap (auto-apply path).
4. Experiments + winner/guardrail + decay holdout.
5. Incident loop (public_02).
6. Load test, crash-recovery script, supervisor view, observability.
7. SUBMISSION.md.

## 12. What changed after building (kept so the reasoning is visible)

- Experiments are per cohort (not one cohort per workflow): voice effects differ by age band, so a pooled test hides them.
  Alpha became 0.10 *per workflow*, split across tests and looks; 0.05 per test would give ~3 false winners per workflow.
- No welcome × voice interaction test (sample cost); marginal effects only. Disclosed in SUBMISSION §13.
- Decay uses a pooled check over cohorts and only the last 3 days; per-cohort holdouts are too thin.
- Governance learned two things from public_02: auto changes must wait while an approval is pending (version bump makes it
  stale), and a supervisor edit means *that dimension is owned by the human* afterwards.
- Caller-ID comparison is stratified by (day, hour) cell; raw window totals blamed a healthy ID for having been used at bad hours.
- Restart does not re-plan a simulated minute that was already planned (`planned_minute`); found by the crash-point script.
- Incident verification judges only calls made after the fix and fails on engagement only when confident (one-sided 95%).
- Decay evidence is pooled across promoted cohorts *and* cohorts where the variant is still a live challenger; a supervisor edit now
  forgets only the touched dimensions (it used to wipe all experiment records and orphan live experiments, found on the seed-777 run).
