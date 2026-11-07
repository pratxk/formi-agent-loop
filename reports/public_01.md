# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.340 | -0.5% | 0.244 | -0.1% | 0.492 | -0.0% | 0% | 72 (57) |
| nw_kyc | 0.362 | -1.4% | 0.241 | -0.4% | 0.592 | -0.8% | 0% | 23 (21) |
| nw_renewal | 0.443 | +6.3% | 0.267 | +1.0% | 0.565 | +4.4% | 16% | 58 (54) |
| ALL | 0.379 | +1.9% | 0.251 | +0.2% | 0.537 | +1.5% | 6% | 153 (132) |

## Changes applied by the controller

7 applied — neutral: 6, beneficial: 1

- ks_cancel v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.003 didΔ=+0.000 → **neutral**
- nw_renewal v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.062 didΔ=+0.000 → **beneficial**
- ks_cancel v4 (controller_approved) dims=['welcome_message'] engageΔ=+0.010 didΔ=+0.000 → **neutral**
- ks_cancel v5 (controller_approved) dims=['voice'] engageΔ=+0.003 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 8,
  "decisions": {
    "approved": 7,
    "rejected": 1,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 1
  },
  "applied_via_approval": 7,
  "auto_applied": 0,
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
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "4": 0.288,
    "5": 0.301,
    "6": 0.303,
    "7": 0.304
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0