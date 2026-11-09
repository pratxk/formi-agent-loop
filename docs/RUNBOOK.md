# Runbook: the outcome-first controller

For whoever is paged while the author is on leave. Read top to bottom once; later use the headings.

## What it is
One Python process (`python -m controller --url <SIM_URL>`, wrapped by `./run.sh`). Every 15 simulated minutes it
advances the simulator, ingests new events into SQLite, lets four planners look at the data, and lets the governance
client send, track and apply the resulting proposals. It only knows what the simulator's HTTP API returns.

```
ingest ──► SQLite ──► planners (calltime, schedverify, callerid, incidents, experiments) ──► governance client ──► sim
```

## Run, test, inspect
```bash
pip install -r requirements.txt
./run.sh http://127.0.0.1:8700                 # state in state/controller.db (override: CONTROLLER_DB=path)
python -m pytest -q                            # 39 tests, ~2s, no simulator needed
python -m controller.view                      # one screen: testing / needs you / changed / incident / loop health
python -m controller.view --json | jq .        # same, machine readable
python -m controller.view --weekly nw_         # change log with reasons (client summary)
python -m controller.why <workflow> <ISO-ts>   # why did this workflow change at T, who approved, what happened after
python -m controller.why --customer <lead_id>  # what did this customer hear, and why was it live
bash scripts_crash_test.sh                     # 3 random kill -9s on public_01, compares with an uninterrupted run
bash scripts_crash_points.sh                   # self-kill at 3 exact instants on public_02
```

## State (everything is in `state/controller.db`, WAL mode)
| table / key | what it holds |
|---|---|
| `meta` `last_chunk`, `hist_chunk` | ingest cursors; committed in the same transaction as the aggregates |
| `meta` `planned_minute` | planners already ran for this simulated minute (restart does not re-plan it) |
| `meta` `exp:<wf>:<cohort>:<dim>` | experiment record: `cur` (live) and `pending` (proposed, not yet seen live) |
| `meta` `inc:<wf>` | incident record: phase, validations paid for, candidate, verification snapshot |
| `meta` `exp_hold:<wf>`, `exp_owned:<wf>` | 48h pause after a supervisor edit; dimensions a human owns (never explored again) |
| `call_agg`, `call_audit`, `leads`, `hist_agg`, `sim_events` | evidence and the per-call audit trail |
| `proposals`, `proposal_log`, `decisions` | every proposal, every transition, and the inputs behind every planner outcome |
| `loop_metrics` | one row per tick: ingest and tick wall time, events |

## On restart (automatic)
1. If the simulator's time is *behind* our store, it is a new world: the old store is renamed `*.old<ts>` and we start clean.
2. Ingest any chunk after the cursor. 3. Reconcile `SUBMITTING`/`APPLYING` proposals against the simulator
(`GET /approvals`, config history). 4. Run planners only if they have not run for this minute. Nothing needs doing by hand.

## Proposal states
`QUEUED → SUBMITTING → SUBMITTED → APPROVED → APPLYING → APPLIED`; exits `REJECTED` (6h cool-off, then a new ID),
`EXPIRED`, `LAPSED`, `STALE`, `SUPERSEDED`, `BATCHED`. A re-ask always has a new ID (`-rN`). Find stuck work with:
```sql
select workflow, kind, state, created_ts, summary from proposals where state in ('SUBMITTING','APPLYING','APPROVED');
select * from proposal_log where proposal_id = '<id>' order by id;
```
`APPROVED` for more than one tick means apply is failing: look at the `note` in `proposal_log` and the apply window
(`apply_by`). `SUBMITTING`/`APPLYING` after a restart resolve themselves on the first tick.

## Alerts (what pages, what does not)
Page: controller process down; ingest cursor not advancing while the simulator advances; credits below the reserve
(20) with an open incident (`meta inc:*`); an `APPROVED` proposal close to `apply_by`; a hallucination incident open >4h.
Do not page: a rejection (a judgement call by design), `STALE` (we re-propose), one 5xx (retried 6 times), a caller-ID
rotation (informational), an experiment with no winner yet.

## What the planners do and the constants that matter
| file | constant | meaning |
|---|---|---|
| `planners/calltime.py` | `MIN_SEG_DIALS=2000`, chi2 p<1e-6, `W_MIN=.25` | when an age band gets its own hour table; no hour ever gets weight 0 |
| `planners/schedverify.py` | `Z_BAD=-3`, `MIN_DIALS_SIDE=400`, up/down weights 1.1/0.9 | when a schedule change is undone (premise reversed) |
| `planners/callerid.py` | `Z_BAD=-3.5`, `REL_BAD=.8` (last 6h), `FLOOR=.05`, `SPARE_PROBE=.1` | when an ID is flagged; share kept on a demoted ID |
| `planners/experiments.py` | `ALPHA=.10` per workflow, `LOOKS=6`, `MIN_N=300`, `MIN_LIFT=.02`, `CHAMPION_SHARE=.4`, `HOLDOUT_SHARE=.15`, `ROLLBACK_Z=-1` | the speed vs false-winner trade-off |
| `planners/incidents.py` | `Z_BAD=3`, `RATIO_BAD=2`, `MIN_CUR_N=150`, `VALIDATE_N=200`, `CREDIT_RESERVE=20` | incident detection and credit spend |
| `governance.py` | `REJECT_COOLOFF_H=6`, `RESERVED_SLOTS=1` | cool-off after a rejection; quota slot kept for incidents |
| `config/client_prefs.json` | per tenant `earliest_hour`, `max_per_24h` | human-supplied client constraints stricter than policy |

## Rules the code will not break (do not "fix" these)
- Customer-facing changes (welcome, voice, prompt, cohorts) always go through approval, even where auto-apply is allowed.
- A supervisor's edit is never reverted. After one, that dimension is owned by the human: no more experiments on it.
- Auto changes are held while an approval is waiting for a decision (a version bump would make it stale).
- Proposal IDs are deterministic. Do not add randomness; the retry-after-crash path depends on it.
- Pickup and engagement are never blended.

## Common situations
- **"Why did X change?"** `controller.why`. **"What did this customer hear?"** `controller.why --customer`.
- **Controller hung on startup in a big scenario.** Startup ingests history first (5.5M events on public_03 takes ~30 s).
- **Credits low.** `select value from meta where key='credits'`; incidents stop spending at the reserve and wait.
- **Wipe state.** Stop the process, move `state/controller.db*`. Learning is lost, but nothing breaks: governance state
  is re-adopted from the simulator via `GET /approvals` only for proposals we still know about, so wiping with an
  approval in flight can leave that approval to expire.
- **Add a planner.** Write `planners/<name>.py` with `plan(db, gov, wf, pol, cur, now)`, call `gov.propose(...)` with a
  deterministic config, add it to `planners/__init__.py`. Planners must be stateless or keep state in `meta` *before*
  any call that spends credits.

## Known limits (see SUBMISSION.md §13)
Single process and single SQLite writer; lead map in RAM; schedule rollback checks only the hour-order premise; welcome × voice gate unit-tested only; decay rollback needs ~3 days of data at dev traffic.
