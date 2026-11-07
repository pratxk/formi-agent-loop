# Outcome-First Agent Loop — Submission

**Name:** Pratik Singh · **Date:** 2026-11-07 (draft) · **Hours actually spent (be honest):** [fill in] · **Language / storage:** Python 3.11, SQLite (WAL) behind one thin `store` module

Run it: `./run.sh <SIM_URL>` (needs `pip install -r requirements.txt`). One process, no other services, no manual steps.
Where things are: `docs/RUNBOOK.md` (for the person who gets paged), `docs/DESIGN.md` (written before coding), `reports/`, `tests/`,
`scripts_crash_test.sh`, `scripts_crash_points.sh`.

---

## 0. TL;DR

- One always-on loop: advance 15 min → ingest events (exactly-once) → planners (call time, caller ID, hallucination incidents, welcome/voice experiments) → governance client (propose, wait, apply, reconcile). Everything it does is written to its own SQLite store first, with the reason.
- **public_01:** ALL conclusion rate +1.9% (nw_renewal +6.3%, ks_cancel/nw_kyc within ±1.5% noise), 0 harmful changes. **public_02:** ALL +17.0%, ks_cancel +50.7% (spam-flagged caller ID rotated within 2h of onset; bad supervisor prompt replaced via an approved proposal, 180 min MTTR, hallucinated calls 86 vs 348 baseline), 0 harmful. **public_03:** ALL +5.8%, 0 harmful, 272 s wall-clock for 5 simulated days.
- Kill -9 / self-kill at six different points (3 random, 3 at the exact instant after a side effect reached the simulator and before we recorded it): all recover to an identical final state.
- Deliberately not built: per-person call time (C4), cross-tenant fairness (C15), a UI (C11 is a CLI/JSON), prompt experiments, interaction test for welcome×voice. Most measured lift came from exploration itself; we are conservative about promoting winners (section 13).

## 1. Assumptions

1. The controller may only use what HTTP returns. We never read `runs/`, import `sim`, or parse scenario files (checked: `grep -r "import sim\|runs/" controller` is empty).
2. **Welcome message and voice act after the call connects**, so they can move engagement but not pickup. Workflows whose *primary* KPI is pickup (nw_kyc) get no welcome/voice experiments; their spend would buy nothing on the primary. If a hidden scenario has welcome affecting pickup, we miss that. This is the biggest single-point assumption.
3. History (pre-D0) holds one welcome, one voice, one prompt. It can teach call-time and caller-ID effects, never welcome/voice.
4. Pickup differs by hour *and* by age band (history shows it strongly: 60+ peaks 10–11h, 18–30 peaks 18h). Language does not matter for hours. We gate cohort creation on a chi-square test, not on this prior.
5. `engagement_rate` is among *connected* calls; `conclusion rate = pickup × engagement`. For call time we maximise engaged-per-dial (yield) subject to the guardrail. This can lower engagement-per-connected slightly while raising conclusions (seen on nw_renewal in public_02: −1.5% engagement, +1.7% pickup, conclusion flat). A judgement call; flagged in section 13.
6. A supervisor's edit always wins. We never revert it; at most we ask (via approval) for something that makes their edit effective, or propose a safety fix.
7. The automated hallucination detector is noisy but unbiased across prompts, so comparing one prompt's flag *rate* with the other prompts' is valid even if absolute rates are inflated.
8. Time is the simulator's. Daily looks fire at 00:00 simulated time; hourly checks at minute 0.
9. Approver hours and latency come from `GET /workflows`; nothing is hard-coded. Client preferences stricter than policy (Kestrel supervisor memo) live in `config/client_prefs.json` and are labelled as human-supplied.
10. `~940 connected calls/day/workflow` (dev scale) means cohorts must be few and large. We cap by data, not by a constant: a segment needs ≥2,000 historical dials to get its own hour table.

## 2. Scope decisions

| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Ingestion at load scale | **Built** | Floor. Cursor and aggregates commit in one transaction per chunk. 19k events/s on one core. | Shard by workflow for 10× (section 8). |
| C2 | Shared cohort object | **Built** | One `cohort id` is used in config, `call_agg`, experiments, view. | Cohort-level lifecycle (merge/split by evidence). |
| C3 | History bootstrap | **Built** (call time, caller ID) | Only use history allows. | Per-cohort caller-ID priors. |
| C4 | Per-person call time | **Cut** | Only 20–40% of leads return; per-person history is too thin. Cohort-level hours capture the signal. | Needs a returning-lead model and a validation scheme; ~1 week. |
| C5 | Caller-ID health | **Built** | Sibling comparison is unconfounded; staged rotation within `max_shift`; probe share. | Spare-ID exploration beyond a fixed 10% probe. |
| C6 | Welcome/voice experiments | **Built (partial)** | Per cohort, Holm-corrected, contemporaneous arms. No welcome×voice interaction test (independent sampling lets marginals be analysed; interaction not tested). | Add interaction test before promotion; adaptive allocation. |
| C7 | Promotion, decay, rollback | **Built (partial)** | Promotion with 15% holdout; pooled decay check on last 3 days. Detection is slow at 8–10 day horizons (section 5). Schedule changes have no automatic rollback. | Post-apply verification for schedule/cohort changes. |
| C8 | Hallucination incident loop | **Built** | Detect → suggest-fixes → validate → propose with evidence → verify. 9 credits per incident. | Second-candidate retry is coded but not exercised on a public scenario. |
| C9 | Governance client | **Built** | The core. State machine, batching, hours/latency/TTL/quota, stale/expired/lapsed paths, crash reconciliation. | — |
| C10 | Honest impact/dry-run | **Built (partial)** | Call-time dry run is a history replay (estimate). Experiment proposals say "no replay possible". Impact for exploration is a flat 0.3 (we claim no gain). | Bootstrap CIs on dry runs. |
| C11 | Supervisor view | **Built as CLI/JSON** | `python -m controller.view`. One screen, no alerts. | A page over the same JSON. |
| C12 | Audit store | **Built** | `controller.why` for config changes and for a single customer. | — |
| C13 | Crash recovery demo | **Built** | Two scripts, six kill points. | — |
| C14 | Loop observability | **Built (partial)** | Ticks, tick time, events, decisions, credits in `view`. No exporter. | Prometheus endpoint over `loop_metrics`. |
| C15 | Cross-tenant fairness | **Cut** | Quotas and approvers are per workflow already; there is no shared scarce resource except credits (200–400, each incident costs 9). | Credit ledger with per-tenant budgets when tenants share a wallet. |

**Principle:** build the thing whose absence breaks a constraint first (governance, audit, crash safety, ingestion), then the levers with the largest measured headroom per unit of risk (caller ID and incidents are large and fast; call time is steady; welcome/voice is large but slow). Cut anything whose value depends on data we do not have (per-person history) or on presentation (UI).

## 3. Stakeholders

### 01 Sales (Nikhil Rao)
- **Accepted:** speed matters. Caller-ID rotation and (where policy permits) other operational changes apply the same tick they are detected, with no approver round trip. Winners are looked at daily. One headline number per client: `reports/` carry conclusion-rate lift per workflow.
- **Pushed back:** a single blended score (product memo forbids it and it hides a pickup/engagement trade). "Looks hesitant": we send at most one request per workflow at a time and batch; peak was 2 requests/24h on public_01/02.
- **Wrong claims:** (a) *"Kestrel has auto-apply for everything"* is false: `ks_cancel` allows only schedule, caller_id, welcome_message, voice, with `max_shift` 0.2, and prompt changes and cohort changes are never auto. (b) *"Northwind is fine with auto-apply on KYC"*: `nw_kyc` has `auto_apply.enabled: false`, and Northwind compliance requires a named approver for anything customer-facing. A QBR remark is not the contract. (c) *"Persuasive prompt wherever available"*: P4 is not an option for `nw_kyc`, and it is exactly the kind of change that needs validation plus approval.
- **Reply:** "Nikhil, here's what you can show on stage: lift in conclusion rate per client, with pickup and engagement alongside, not blended. Caller-ID problems get fixed in about two hours with no human in the loop, which is a good story. Two corrections before the summit: Kestrel's auto-apply covers only call timing, caller ID, welcome and voice, capped at 20% shifts, and Northwind KYC has auto-apply off in the signed policy. We still send customer-facing changes for approval everywhere; that's what keeps those clients. P4 isn't available on KYC and goes through validation first."

### 02 Northwind Compliance (Meera Iyer)
- **Accepted:** (1) customer-facing changes are never auto-applied, even where policy would permit it; welcome/voice/prompt always go through a named approval. (2) Prompt changes carry a `validation_run_id` and our own pass-rate check before submission. (3) *"Why did this customer hear that?"*: `python -m controller.why --customer <lead_id>` answers in seconds from our store (variant, cohort, config version, who set it, why). (4) Slow and right: significance bars are strict, futile arms are dropped at z<−2, nothing goes live without approval. (5) We submit only inside approver hours with enough time left for the decision to land that day.
- **Wrong or incomplete in the memo:** it says call timing and caller ID are covered by the signed policy. That holds for `nw_renewal` only (auto-apply for schedule + caller_id, `max_shift` 0.5). For `nw_kyc` auto-apply is **disabled**, so even timing and caller-ID changes need an approval there; the controller routes them that way. Also, cohort definitions are not in the auto-apply list, so our timing change (which creates cohorts) goes through approval on both workflows.
- **Tell them honestly:** an experiment shows customers variants they have not heard before; each experiment start is itself an approval request, and a bad arm can reach a cohort for days before we drop it. We cannot make that zero.
- **Reply:** "Meera, agreed on all five. Welcome, voice and script changes always go to your approvers, with validation evidence on script changes; nothing customer-facing is auto-applied. Timing and caller-ID changes on renewals may auto-apply under your policy and will appear in the weekly summary (`controller.view --weekly`). One correction: KYC has auto-apply disabled in your policy, so we ask for approval there too. For IRDAI, we can show per customer which variant they heard and why within seconds. Experiments do expose cohorts to new variants, but only after your approval and with arms dropped when clearly worse."

### 03 Platform & SRE (Kabir Menon)
- **Accepted:** kill test (shown, not promised): section 9. Idempotency everywhere: deterministic proposal IDs, `409 duplicate_proposal_id` adopted, cursor in the same transaction as aggregates. Actionable alerts only (section 10). Loop health without logs: `controller.view`. No new stateful infrastructure: one SQLite file; Postgres is the seam (his stack already runs it).
- **Pushed back / costs:** SQLite is single-writer and per-box: fine for a loop that is the only writer, not for a hot standby. The 10-minute budget is met (54 s per simulated day), but on my laptop, not a 4 vCPU/8 GB Linux box; I report CPU (82 s user + 40 s sys for 5 days) so he can scale it.
- **Wrong claims:** none in the memo. The "we deploy at 21:30" case is covered: approvals waiting at the simulator survive a restart (we reconcile from `GET /approvals`).
- **Reply:** "Kabir, kill test: `scripts_crash_points.sh` kills the process right after a side effect reached the sim and before we recorded it (submit, apply, auto-apply), then restarts; final proposals and config versions match an uninterrupted run. Retries are idempotent via deterministic proposal IDs. State is one SQLite file, no new service; swapping to your Postgres is one module. public_03: 272 s for 5 simulated days (54 s/day) on a laptop, 1.1 GB peak. First thing to break at 10× is single-core ingest; shard by workflow. Health: `python -m controller.view`."

### 04 Kestrel Operations Supervisor (Anita Desai)
- **Accepted:** we never undo her prompt edits: after her bad-prompt event on public_02 we *proposed* a replacement through her approval queue, we did not overwrite. After any supervisor edit the experiment on that dimension stops and is never restarted (ownership). One request containing all changes (batching), "two a day, never before 11:00": implemented as a client preference stricter than policy (`config/client_prefs.json`). One screen: `controller.view`.
- **Wrong or stricter than policy:** policy allows 4 requests/24h and approver hours 10–19; her "2 a day, not before 11" is stricter, honoured voluntarily and labelled human-supplied.
- **Conflict we did not fully resolve:** she wants to be told *before anything changes on a running campaign*. Welcome/voice/prompt/cohort changes wait for her. Caller-ID rotation does not: when a number is spam-flagged, waiting costs about 65% of pickups per hour, so it auto-applies (policy-permitted) and is reported in the change log. I would ask her to confirm that exception.
- **Reply:** "Anita, your prompt edits are never reverted. If one looks unsafe we send you a proposal with validation evidence and wait for you. After you edit something we stop experimenting on that setting. You get at most two requests a day, never before 11:00, with everything batched in one. One thing to confirm with you: if a caller ID is flagged as spam, we rotate it immediately without asking (your policy permits it) because every hour of waiting loses most pickups, and we list it on your screen. Everything a customer hears waits for you."

### 05 Product (Tara Kulkarni)
- **Accepted:** separate KPIs (reports and view show them separately; no blended score). One cohort object (`cohort id` is the key in config, aggregates, experiments, view). Min cohort size/window picked and costed (section 5). Proposals carry impact score, diff, affected leads, dry run. Expired = rejected: re-queued with a fresh ID and fresh data, never resubmitted. Only what happened counts: arms are compared only over config versions in which both were live. A winner changes that workflow's config only.
- **Pushed back:** per-person call time as "P0 for the summit": cut, with reasons (C4). Consolidated view: delivered as CLI/JSON.
- **Wrong or unclear:** "impact score must mean something": it does only for call-time (predicted yield lift ÷ 15%) and promotions (observed lift × cohort share ÷ 3pp). For exploration starts it is a flat 0.3 because we claim no expected gain; incidents and rollbacks are 1.0.
- **Reply:** "Tara, shipped: separate pickup/engagement everywhere, one cohort ID used by config, experiments and the view, proposals with impact, diff, affected leads and dry run, expired proposals re-queued with fresh data. Cohort size: 300 connected calls per arm per cohort, daily looks, which costs us speed (only large effects are promoted within a week) to avoid false winners. Per-person call time is cut: only 20–40% of leads return, so there isn't enough per-person history; cohort-level hours capture most of it. The supervisor view is a one-screen CLI/JSON today; a page can sit on the same data."

### 06 CTO (Aryamann)
- **Accepted:** choosing is the job, explainability over size, spend credits where they change a decision (9 per incident, 0 elsewhere), design for the scale envelope, docs for the engineer who gets paged (`docs/RUNBOOK.md`).
- **Said no to:** per-person call time (Product), UI (Product), a faster-than-safe promotion rule (Sales), blanket auto-apply (Sales), cross-tenant credit fairness (nobody asked; no shared scarce resource yet), prompt experiments (validation measures only safety).
- **Wrong claims:** he says some memos are wrong; confirmed against `GET /workflows`: Sales on Kestrel/Northwind auto-apply; Compliance on KYC "operational covered"; Ops supervisor's limits stricter than policy.
- **Reply:** "Aryamann, I cut per-person call time, the UI, cross-tenant fairness and prompt experiments, and said no to blanket auto-apply and faster-than-safe promotion. What's in: governance, audit, crash safety, caller-ID and hallucination incident response, per-cohort welcome/voice experiments with holdout and decay checks. Credits: 9 per incident, nothing else. At 10× the first thing that breaks is single-core ingest and the in-memory lead map; plan is sharding by workflow. Memos that contradicted the signed policies lost to the policies. Details in section 8 and 13."

## 4. Architecture

```
            ┌────────────────────────── one process, one loop, one tick = POST /sim/advance(15 min) ─────────────────────────┐
 sim ─────► │ ingest (tx per chunk: aggregates + audit rows + cursor)                                                          │
 /events    │   │                                                                                                              │
            │   ▼                SQLite (WAL) = our memory and audit trail                                                       │
            │ planners (stateless, recompute from store each tick)                                                              │
            │   calltime · callerid · incidents · experiments ──► gov.propose(deterministic id, impact, diff, dry run)           │
            │                                                          │                                                        │
            │ governance client:  QUEUED ─► (auto: PUT config) or (approval: POST /approvals ─► poll ─► POST apply)            │
            │   reconcile at start, one approval in flight per workflow, quota/hours/TTL/apply-window aware                      │
            └───────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
 views:  controller.view (one screen) · controller.why (audit: change or customer) · scripts_crash_*.sh
```
- **State (all in `state/controller.db`):** `meta` (cursors, experiment records, incident records, holds), `call_agg` (the experiment/detection grain), `hist_agg` (pre-D0, kept apart so it can never be mistaken for experiment evidence), `call_audit` (one row per call), `leads`, `sim_events`, `proposals`, `proposal_log` (every transition), `decisions` (inputs and thresholds behind every planner outcome), `loop_metrics`.
- **Restart:** (1) open the store; if the simulator is *behind* our stored time, it is a new world: rename the store and start clean. (2) Ingest any chunks after the cursor (exactly-once: the cursor commits with the data). (3) `gov.tick` reconciles `SUBMITTING`/`APPLYING` proposals against `GET /approvals` and config history. (4) Planners run only if they have not already run for this simulated minute (`planned_minute`). The loop resumes.
- **What is lost on a crash:** nothing durable. At most one credit-spending call whose result was not yet saved (2 credits).

## 5. The learning loop

- **Cohorts.** Created once, from history, only if a chi-square test says the hour profile truly differs by segment (p<1e-6, each segment ≥2,000 dials). On the data, `age_band` passes by a huge margin and `lang` does not, so four age cohorts (18–30, 31–45, 46–60, 60+) plus `default`. Hour weights are shrunk toward the segment mean, floored at 0.25 (no hour is zeroed) and normalised. Fragmentation is limited by the gate, not by a fixed cap; the cost is that traffic per cohort is ~¼ of the workflow, so experiments are slow (below).
- **Experiment design.** Per (cohort, welcome or voice). Champion keeps 40%, challengers split 60%. Welcome and voice are sampled independently by the simulator, so each dimension is analysed on its marginal effect with the other dimension averaged out. **Not done: the interaction test** (section 13). Allocation does not adapt; it only changes on a decision.
- **Deciding a winner.** Daily look. One-sided two-proportion z-test on engagement among connected calls, challenger vs champion, using only config versions in which *both* arms were live (so a trend or a novelty effect cannot masquerade as an arm effect). Holm step-down across challengers. Alpha is **0.10 per workflow**, split over all (cohort × dimension) tests and 6 looks (Bonferroni-style), so the family-wise false-winner rate is ≤10% per workflow, conservatively. Extra gates: lift ≥2pp, ≥300 connected per arm, the late half of the data must retain ≥40% of the lift (stops a fading novelty effect from being promoted), pickup guardrail one-sided CI. A clearly worse arm (z<−2) is dropped immediately.
- **Cost of this choice (what Tara asked for).** With 300 per arm the standard error of a difference is about 4pp, and the bar after multiplicity is z≈2.9, so only effects of roughly ≥10pp are promoted. Over 8 days this promoted few winners; the gain we measured on nw_renewal (+4.4% engagement) came mostly from exploration itself moving traffic off a champion that data later showed was not best, not from promotion. Faster means more false winners; we chose few false winners.
- **Decay and rollback.** After a promotion the old champion keeps a 15% holdout. Daily, we pool every cohort where the same variant was promoted (Stouffer-combined z, weight √n) over the last 3 days only, and roll back (to the old champion) when pooled z<−1 with ≥100 holdout calls. Pooling exists because each cohort's holdout is tiny. Rollback goes through approval at priority 1 (customer-facing). Detection takes about 3 days in the worst case; **we did not see a promoted variant decay on a public scenario**, so this path is covered by unit tests, not by an end-to-end run.
- **Call time and caller ID.** History bootstraps call time (hour weights per age cohort, one proposal, plan once; predicted yield lift via replay of history; guardrail precheck). Caller ID is monitored hourly, comparing each ID with its siblings over 24h (allocation is independent of lead and hour, so no baseline is needed): act when z<−3.5 over 24h **and** pickup is <80% of expected over the last 6h, with ≥200 dials each side. The comparison is stratified by (day, hour) cell (Mantel-Haenszel style): allocation changes during the window and pickup depends on hour, so raw totals blamed a healthy ID for having been used at bad hours (this produced a false D1 rotation on public_02 before the fix; there is a unit test for it). The ratio uses the recent window because a 24h average dilutes a fresh collapse and delayed detection by ~3h. Rotation is staged within 0.9×`max_shift` per change, the demoted ID keeps 5% so recovery is visible, a spare ID gets a 10% probe, recovery is detected when z>−1.5. If *all* IDs look bad together it is not a caller-ID problem and we do nothing.
- **Guardrail.** Call-time proposals are pre-checked (predicted guardrail drop must be under half the cap). Experiment winners need the one-sided CI on the pickup guardrail to clear the cap. Prompt fixes are verified on engagement against the other prompts after they go live.

## 6. Governance

- **State machine.** `QUEUED → SUBMITTING → SUBMITTED → APPROVED → APPLYING → APPLIED`, with exits `REJECTED` (6h cool-off, then a new ID), `EXPIRED` (counts as a rejection; re-queued with a fresh ID and fresh data), `LAPSED` (apply window missed; re-queue), `STALE` (409: re-propose from the current version), `SUPERSEDED` (older queued proposal of the same kind, or base moved), `BATCHED` (merged into a request; follows its terminal state).
- **Timing.** Submit only inside approver hours, and only if the decision can land the same day (latency max) or it is the first 15-min slot of the day. One request in flight per workflow. Batch disjoint-dimension proposals into one request. Quota: 1 of the daily requests is reserved for priority>0 (incidents, caller ID, rollbacks), so an optimisation can never use the last slot. Client prefs (Kestrel) can only tighten.
- **Auto-apply.** Used only for `caller_id` and `schedule` and rollbacks where policy allows it, within 0.95×`max_shift`. We do **not** auto-apply anything customer-facing even where permitted (Kestrel welcome/voice), because the client asked to know and compliance wants a named approver. A 403 from the simulator falls back to the approval path.
- **A lesson from public_02.** An automatic caller-ID step changes the config version, which makes any approval waiting for a decision stale (409). The safety-fix approval went stale twice before we held non-urgent auto changes while an approval is waiting. After that fix, MTTR on the bad prompt went from ~30h to 3h.
- **Impact, affected leads, dry run.** Affected leads = the workflow's daily batch size (an upper bound, not per-cohort). Impact: call-time = predicted yield lift ÷ 15%, capped at 1; promotions = observed lift × cohort share ÷ 3pp; exploration = 0.3; incident/rollback = 1.0. Dry run: call time = replay of pre-D0 history with the new hour weights (estimate: assumes hourly slot supply is unchanged); experiment promotions = observed arm comparison with counts (labelled "observed, not a forecast"); exploration = "none, there is nothing to replay"; prompt fixes = the validation run (safety only).
- **Supervisor edit mid-experiment.** Detected from `config_changed` with `actor: supervisor` and from live allocation not matching ours. Their edit is never reverted. Experiment records on the touched dimensions are deleted, experiments pause for 48h, and the dimension is owned by the human (we never explore it again). If our per-cohort overrides would mask their default-level edit, we ask (kind `experiment_reset`) to remove the overrides so their edit takes effect everywhere. On public_02 (nw_renewal, D5 voice edit) this turned a "harmful" exploration change into a neutral outcome.

## 7. Incidents

- **Hallucination detection.** For the prompt now live: connected ≥150 calls, and its flag rate vs the pooled rate of the other observed prompts (≥300 connected) must give z>3 **and** ≥2× the reference. With no reference, an absolute 4%. Expected false positives: the one-sided p at z=3 is 1.3e-3 per look; with a 2× ratio gate on top and correlated 15-minute looks the expected false-trigger rate is well under one per workflow-run; a false trigger costs 9 credits and a request to the approver (never an automatic change).
- **Picking a fix.** One `suggest-fixes` call (3 credits) to learn what each alternative is; exclude near-verbatim scripts (summary matches `verbatim|word-for-word|compliance script|strict`, the "read the script" failure); validate up to 3 candidates at n=200 (2 credits each; ordered observed-in-production first); keep those at or above the policy's pass rate; prefer observed prompts whose engagement is not clearly worse. Validation measures safety only, so engagement is judged from production data, never from validation.
- **Credit spend:** 9 per incident (1 suggest + 3 validations), reserve 20 credits never spent. Total on public_02: 9 of 200.
- **Verify after apply:** judged on calls made *after* the fix went live (≥200 connected): flag rate must fall below half the offender's (or 1%), and engagement must stay within 2pp of what the other prompts achieved. The engagement test is one-sided at 95% with a 2pp guard: a first version compared point estimates and wrongly failed a good fix on 217 noisy calls. Failure marks the prompt tried and the next candidate is used.
- **Timeline (public_02, ks_cancel):**
  - D2 10:00 (2027-01-06): caller ID D2 spam-flagged. D2 12:00: first hourly check with enough data flags it; staged auto rotation starts (6 applied steps). Flagged share of dials: 18.5% on D2, ~5% from D3 on (the 5% is our deliberate probe share).
  - D3 11:30 (2027-01-07): supervisor ships prompt P2 (aggressive script). 13:30 detected (P2 flagged 4.6% vs 1.6% on the other prompts, z=3.18, n=194). `suggest-fixes` listed P2, P5, P4; P5 excluded as a near-verbatim script; P1, P3, P4 validated at 98.5%, 99.0%, 97.5%; P1 chosen (passed, and observed in production with 49.1% engagement). Proposal submitted 13:30, approved 14:30, applied 14:30. Verified 17:15 over 217 calls made after it went live: flag rate 1.4% (was 4.9%), engagement 46.5% vs 49.1% for P1 before the incident. That is 2.6pp lower but inside the ~3pp standard error of 217 calls, and the check only fails the fix when it is confident engagement dropped past the 2pp guard, so it is recorded as resolved. Grader: MTTR 180 min; hallucinated calls 86 vs 348 baseline; credits 9 of 200.
  - D5 15:00 (nw_renewal): supervisor sets default voice to V2. Experiments on voice reset and stopped for good (owned); proposal to remove 4 cohort overrides so V2 applies everywhere, submitted the next morning inside approver hours (approver hours 10–19; the edit landed at 15:00 when the decision could not land the same day), applied 2027-01-10 13:30. V2 was never reverted.

## 8. Scale

Measured on `public_03_load` (12 workflows, ~650k dials/day, 5 simulated days) on a laptop, uncontended (caffeinate, no other load), one process:

| Metric | Value |
|---|---|
| Wall-clock, 5 simulated days (incl. 5.5M history events) | **272 s** (54 s per simulated day; budget 600 s) |
| Events ingested | 9.7M (5.5M history + 4.2M live) |
| Ingest throughput | ~21k events/s on one core (200 s of ingest for 4.2M live events) |
| Decision lag | one tick: mean 0.5 s, max 3.3 s of wall time per 15-min tick |
| Peak memory | 1.12 GB RSS (1.73 GB peak footprint) |
| CPU | 82 s user + 40 s sys total: about 1 core |
| Store size | 874 MB (3.24M `call_audit` rows, indexes included) |

(raw: `reports/load_public_03.txt`; a first, interrupted run on a sleeping laptop showed 8,600 s of wall time but the same 83 s of CPU, which is why the clean run uses `caffeinate`.)

- **What breaks first at 10×:** (1) single-core ingest: ~40 s/day today, ~400 s/day at 10×, close to the 600 s budget; (2) the in-memory `lead → (lang, age)` map (~1.5 GB at 10× leads; move to a keyed store lookup); (3) the audit table at ~9 GB per 5 days: partition by day and keep 30 days. Per-tick HTTP is not a problem (2 GETs per workflow per tick).
- **Partitioning:** workflows are independent (quotas, approvers, credits and configs are per workflow), so shard by tenant or workflow: N processes, each with its own SQLite file, each given a list of workflow IDs. Cross-shard coordination is only credits if tenants share a wallet (C15, cut).
- **Not measured:** a 4 vCPU / 8 GB Linux box. My laptop is faster per core than a typical cloud vCPU; budget a ×2 margin and you still have ~4× headroom to the 10-minute bar.

## 9. Reliability

- **Method (two scripts, both in the repo):** `scripts_crash_test.sh` kills the controller with `kill -9` three times at random points of a public_01 run, restarts it each time, then compares call aggregates, audit rows, the ingest cursor, proposal counts by state, and the final config versions with an uninterrupted run. `scripts_crash_points.sh` does the harder case on public_02: the process kills itself (`os._exit(137)`) **once** at each of three instants, right after a side effect reached the simulator and before we recorded it: `after_submit` (approval created), `after_apply` (approved config applied), `after_auto_put` (auto-apply landed).
- **Result:** all six runs end in the identical state (`PASS: identical`; log in `reports/crash_recovery.txt`). The first version of the third scenario *failed*: on restart the planners ran again for the same simulated minute, taking two caller-ID steps in an hour instead of one. Fixed with `planned_minute`; the test is what found it.
- **Idempotency keys:** proposal IDs are `sha1(workflow, base_version, kind, config)`, with `-rN` appended only to re-queue a previously expired/rejected/stale one (so a retry after a crash adopts via `409 duplicate_proposal_id`, a deliberate re-ask uses a new ID). Cursor commits with aggregates in one transaction (`INSERT OR IGNORE` on event IDs as a belt-and-braces).
- **Lost on a crash:** at most 2 credits (a validation run whose result had not been saved).
- **Simulator 4xx/5xx:** transport errors and 5xx retry with exponential backoff (6 tries); 4xx is returned to the caller. `/sim/advance` is never retried blindly (a repeated advance would skip time); 409 means the scenario is over. 402 (no credits) is guarded by a reserve and skips the incident loop rather than crashing.

## 10. Audit & observability

```
$ python -m controller.why ks_cancel 2027-01-07T14:30:00+05:30
ks_cancel -> config v7 at 2027-01-07T14:30:00+05:30 by controller_approved; dims=['prompt_version']
  Why: Safety fix: replace prompt P2 with P1 (hallucination flags 4.6% vs 1.6% on other prompts)
  Rationale: Automated QA flags 4.6% of connected calls under P2 vs 1.6% under other prompts (z=3.18, n=194). P1 passed validation at 98.5% ...
  Impact score 1.00, affected leads ..., route=approval, kind=incident_fix
  Evidence: {"validation_run_id": "val_00001"}
  Lifecycle:  QUEUED → SUBMITTING → SUBMITTED → APPROVED → APPLYING → APPLIED  (with timestamps)
  Approver decision: approval apr_..., reasons=[]
  Before (v6): ... pickup, engagement   After (v7): ...
```
```
$ python -m controller.why --customer ks_cancel:l00024020     # IRDAI-style question
  2027-01-06T13:02  cohort=age_band_31_45 welcome=W3 voice=V1 caller_id=D2 prompt=P1 ...
      why live: config v4 set by controller_auto: Rotate calls away from caller ID D2 ... because ...
$ python -m controller.view            # the one screen
$ python -m controller.view --weekly nw_   # change log with reasons, the summary Northwind asked for
```
(Real outputs for the public_02 run are saved in `reports/audit_examples.txt`.)

- **Would page:** (1) ingest cursor not advancing for >N ticks while the simulator advances (loop stuck; message: last chunk, last error); (2) controller process down; (3) credits below the reserve with an open incident; (4) a proposal approaching `apply_by` without being applied (we apply the tick an approval appears, so this means the loop is not running); (5) hallucination incident open >4h.
- **Would not page:** rejections (a judgement call by design), a stale proposal (we re-propose), a single failed 5xx (retried), a caller-ID rotation (informational), an experiment with no winner.

## 11. Results

Tables are from `reports/public_01.md`, `public_02.md`, `public_03.md` (the `sim grade` output vs the no-op baseline run of the same scenario).

**public_01** (all numbers vs baseline)

| workflow | conclusion Δ | pickup Δ | engagement Δ | gap captured |
|---|---|---|---|---|
| ks_cancel | −0.5% | −0.1% | −0.0% | 0% |
| nw_kyc | −1.4% | −0.4% | −0.8% | 0% |
| nw_renewal | **+6.3%** | +1.0% | +4.4% | 16% |
| ALL | +1.9% | +0.2% | +1.5% | 6% |

**public_02**

| workflow | conclusion Δ | pickup Δ | engagement Δ | gap captured |
|---|---|---|---|---|
| ks_cancel | **+50.7%** | +34.7% | +6.3% | 40% |
| nw_kyc | +4.1% | +3.9% | +0.1% | 0% |
| nw_renewal | −0.9% | +1.7% | −2.4% | −3% |
| ALL | +17.0% | +14.7% | +0.3% | 22% |

**public_03** (12 workflows): ALL conclusion +5.8%, pickup +1.3%, engagement +3.6%, gap captured 23%; 35 changes applied, 18 beneficial, 17 neutral, **0 harmful**; renewal workflows +5.8% to +11%, cancel +6.4% to +10.3%, KYC +0.3% to +1.3%.

- **Where we helped:** caller-ID incident and the bad prompt on public_02/ks_cancel (the large number); exploration on renewal workflows (+4–9% engagement); call time on all (+1–2% pickup).
- **Where we did not:** ks_cancel on public_01 (−0.5%): our data showed W3 clearly worse than W1 only after about four days of exploring it at 30% share in each cohort, and that cost about what the other levers gained. nw_kyc (−1.4% on public_01): only a schedule change was applied and the grader labels it neutral; with ~3k connected calls per workflow a ±2% relative difference is inside sampling error (seed-to-seed variance not measured).
- **Harmful:** none in the final runs. An earlier version made one harmful change on public_02 (restarting an experiment on a dimension the supervisor had just set); fixed by "owned dimensions" (section 6).
- **What I'd change:** adaptive allocation (shift traffic toward leading arms between looks, keep the formal promotion test) to cut the cost of exploration; a post-apply check on schedule changes; test-and-learn on the engagement impact of hour weights (we assume it is flat).

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Thompson sampling / bandit allocation | Faster use of what we learn | The brief wants deterministic winner selection; bandits make "what won" unreviewable. Kept fixed allocation; adaptive reweighting is future work. |
| Postgres or Redis for state | SRE already runs them | Extra moving part for a single-writer loop; SQLite file survives kill -9 and `run.sh` works on a clean box. The `store` module is the seam. |
| Per-person call-time | Product's headline ask | Too little per-person history (20–40% return). |
| LLM to pick winners or write variants | Could find better prompts | Brief forbids LLM as decider; validation measures safety only. LLM used only to list candidate prompts, selection is a deterministic filter. |
| Auto-apply customer-facing changes where permitted (Kestrel) | Speed (Sales) | Client wants to be told first; compliance culture; speed is not worth a named-approver breach. |
| One big cohort per segment combination (e.g. senior × Tamil) | Finer personalisation | 940 connected calls/day cannot support it in 8 days. |
| Interaction test for welcome × voice | Textbook for factorial designs | Needs 4× the sample for any power; cut and disclosed. |
| Reverting the supervisor's prompt automatically | Fastest MTTR | Violates the governance boundary. We propose; a human approves. |
| Alpha 0.05 per test | Standard | With ~10 tests per workflow and 6 looks, that gives about 3 false winners per workflow. Used 0.10 per workflow instead. |

## 13. Known weaknesses

1. **Exploration is slow and costs something.** Experiments run per age cohort with 300 connected calls per arm minimum; in 8–10 days few winners get promoted. On public_01, ks_cancel lost about as much as it gained.
2. **No welcome × voice interaction test.** Independence of sampling lets marginals be read, but a bad combination of two individually good arms is possible.
3. **Decay rollback is untested end to end** on a public scenario (only unit tests). It also needs about 3 days to react at dev-scale traffic.
4. **No post-apply check or rollback for schedule/cohort changes.** They are neutral per the grader, but nothing in the loop would catch a harmful one.
5. **Pickup-primary workflows get no welcome/voice experiments** (assumption 2).
6. **Call-time success metric** is yield (engaged per dial), which can shave engagement-per-connected while raising conclusions (assumption 5).
7. **Cohorts are decided once from history** and never re-split or merged; if the population shifts, they do not follow.
8. **Seed-to-seed variance not measured.** Single-workflow differences of ±2% on public_01 are within sampling error at ~3k connected calls, so they are not signal either way; I did not run multiple seeds.
9. **Hours spent and a live 4 vCPU/8 GB measurement are missing**; the load numbers come from a laptop.
10. **Memory:** the `lead → features` dict is in RAM and grows with leads.
11. **Second-candidate retry for failed incident fixes** is implemented but not exercised.

## 14. If this went to production at Formi next month

1. Week 1, day 1–2: move the store behind the `store` interface to Postgres (their cluster), keep the SQLite adapter for tests. Add the Prometheus endpoint over `loop_metrics`, the five alerts in section 10, and a deploy-time smoke test (`scripts_crash_points.sh`).
2. Day 3–4: replace the simulator tick with a real scheduler: a clock-driven loop that reads from the production event stream, with the same exactly-once cursor; keep `planned_minute` semantics per workflow.
3. Day 5–7: shard by workflow (N workers, leader election via a Postgres advisory lock so a recycled node resumes the same shard). Move the lead map out of RAM.
4. Week 2: add adaptive allocation and the interaction test; schedule/cohort post-apply verification; per-tenant credit budgets (C15).
5. Week 2: a shadow mode where proposals are logged but not sent, run on two real workflows for a week; compare against what supervisors actually did before any live approval requests.
6. Week 2: put `controller.view` behind a small page for supervisors and wire the weekly Northwind summary to email.
