# Grade report — public_03_load

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| arav_cancel_2 | 0.372 | +10.3% | 0.248 | +1.8% | 0.530 | +6.7% | 41% | 414 (409) |
| arav_kyc_1 | 0.360 | +0.3% | 0.217 | +1.1% | 0.594 | -1.0% | 0% | 349 (352) |
| arav_renewal_0 | 0.447 | +10.4% | 0.267 | +1.1% | 0.594 | +8.4% | 45% | 433 (408) |
| kest_cancel_2 | 0.361 | +7.3% | 0.246 | +1.5% | 0.520 | +4.2% | 29% | 403 (418) |
| kest_kyc_1 | 0.359 | +1.0% | 0.217 | +1.3% | 0.595 | -0.5% | 0% | 352 (370) |
| kest_renewal_0 | 0.428 | +6.6% | 0.268 | +1.1% | 0.566 | +3.9% | 20% | 415 (403) |
| nort_cancel_2 | 0.363 | +6.9% | 0.248 | +1.5% | 0.518 | +3.9% | 31% | 422 (368) |
| nort_kyc_1 | 0.359 | +1.0% | 0.217 | +1.0% | 0.595 | -0.1% | 0% | 382 (318) |
| nort_renewal_0 | 0.451 | +11.1% | 0.267 | +1.2% | 0.598 | +9.2% | 48% | 440 (419) |
| tide_cancel_2 | 0.365 | +6.4% | 0.248 | +1.4% | 0.522 | +4.3% | 39% | 410 (401) |
| tide_kyc_1 | 0.361 | +1.3% | 0.218 | +1.5% | 0.594 | -0.7% | 0% | 329 (376) |
| tide_renewal_0 | 0.428 | +5.8% | 0.266 | +0.9% | 0.568 | +4.0% | 22% | 404 (425) |
| ALL | 0.388 | +5.8% | 0.244 | +1.3% | 0.565 | +3.6% | 23% | 4753 (4667) |

## Changes applied by the controller

35 applied — neutral: 17, beneficial: 18

- arav_cancel_2 v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nort_renewal_0 v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_cancel_2 v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- arav_renewal_0 v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nort_cancel_2 v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- kest_cancel_2 v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- arav_cancel_2 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- nort_renewal_0 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.061 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- nort_cancel_2 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- arav_renewal_0 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.061 didΔ=+0.000 → **beneficial**
- arav_kyc_1 v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nort_kyc_1 v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_kyc_1 v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- kest_kyc_1 v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_cancel_2 v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.053 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.058 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.004 didΔ=+0.000 → **neutral**
- nort_renewal_0 v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.052 didΔ=+0.000 → **beneficial**
- kest_renewal_0 v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.061 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.043 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.077 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.036 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.034 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.069 didΔ=+0.000 → **beneficial**
- tide_renewal_0 v2 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_cancel_2 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.036 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.035 didΔ=+0.000 → **beneficial**
- tide_renewal_0 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.061 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v5 (controller_approved) dims=['voice'] engageΔ=+0.021 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v6 (controller_approved) dims=['welcome_message'] engageΔ=+0.006 didΔ=+0.000 → **neutral**
- kest_cancel_2 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.027 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v6 (controller_approved) dims=['welcome_message'] engageΔ=+0.009 didΔ=+0.000 → **neutral**
- tide_renewal_0 v4 (controller_approved) dims=['welcome_message'] engageΔ=+0.036 didΔ=+0.000 → **beneficial**
- tide_renewal_0 v5 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.051 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 40,
  "decisions": {
    "approved": 35,
    "rejected": 5,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 5
  },
  "applied_via_approval": 35,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nort_renewal_0": 2,
    "nort_kyc_1": 1,
    "nort_cancel_2": 2,
    "arav_renewal_0": 2,
    "arav_kyc_1": 1,
    "arav_cancel_2": 2,
    "tide_renewal_0": 2,
    "tide_kyc_1": 1,
    "tide_cancel_2": 2,
    "kest_renewal_0": 1,
    "kest_kyc_1": 1,
    "kest_cancel_2": 1
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
  "nort_renewal_0:welcome_message=W3": {
    "3": 0.85,
    "4": 0.848
  },
  "arav_renewal_0:welcome_message=W3": {
    "3": 0.849,
    "4": 0.848
  },
  "kest_renewal_0:welcome_message=W3": {
    "4": 0.299
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0