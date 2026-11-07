# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.355 | +50.7% | 0.244 | +34.7% | 0.497 | +6.3% | 40% | 86 (348) |
| nw_kyc | 0.389 | +4.1% | 0.244 | +3.9% | 0.609 | +0.1% | 0% | 24 (29) |
| nw_renewal | 0.429 | -0.9% | 0.267 | +1.7% | 0.541 | -2.4% | -3% | 54 (54) |
| ALL | 0.387 | +17.0% | 0.252 | +14.7% | 0.534 | +0.3% | 22% | 164 (431) |

## Changes applied by the controller

12 applied — neutral: 3, beneficial: 9

- nw_renewal v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.038 didΔ=+0.000 → **beneficial**
- ks_cancel v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.021 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.181 → **beneficial**
- ks_cancel v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.146 → **beneficial**
- ks_cancel v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.063 → **beneficial**
- ks_cancel v7 (controller_approved) dims=['prompt_version'] engageΔ=+0.102 didΔ=+0.000 → **beneficial**
- ks_cancel v8 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.055 → **beneficial**
- ks_cancel v9 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.054 → **beneficial**
- ks_cancel v10 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.019 → **neutral**
- nw_renewal v5 (controller_approved) dims=['voice'] engageΔ=+0.044 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 8,
  "decisions": {
    "approved": 6,
    "rejected": 2,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 2
  },
  "applied_via_approval": 6,
  "auto_applied": 6,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 2,
    "nw_kyc": 1,
    "ks_cancel": 1
  }
}
```

## Incidents

```
[
  {
    "type": "did_flagged",
    "workflow": "ks_cancel",
    "at": "D2 10:00",
    "flagged_did_share_by_day": {
      "2": 0.185,
      "3": 0.055,
      "4": 0.052,
      "5": 0.051,
      "6": 0.047,
      "7": 0.055,
      "8": 0.051,
      "9": 0.05
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": 180,
    "fixed_by": "controller_approved"
  },
  {
    "type": "supervisor_edit",
    "workflow": "nw_renewal",
    "at": "D5 15:00"
  }
]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "2": 0.321,
    "3": 0.287,
    "4": 0.295,
    "5": 0.296,
    "6": 0.302,
    "7": 0.301,
    "8": 0.303,
    "9": 0.293
  }
}
```

Credits used: 9 · validation runs: 3 · LLM fix calls: 1