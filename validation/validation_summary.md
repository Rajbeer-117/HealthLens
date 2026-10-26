# HealthLens Deliverable 2: scenario validation

Patients analysed: **900** (errors: 0) in 356 s

## Scenario status counts

| Scenario | Several related signals | Some related signals | No notable pattern | Not enough data | Not applicable |
|---|---|---|---|---|---|
| metabolic | 270 | 296 | 334 | 0 | 0 |
| cardiovascular | 220 | 439 | 241 | 0 | 0 |
| pcos | 8 | 13 | 282 | 0 | 597 |

## Sanity check against recorded medical history

Share of patients whose history records a related condition and whose scenario showed "some" or "several" related signals, compared with patients without that history entry.

| Scenario | History terms | With history: n / % with signals | Without history: n / % with signals |
|---|---|---|---|
| metabolic | prediabetes, type 2 diabetes, borderline glycemic | 462 / 82% | 438 / 42% |
| cardiovascular | hypertension, hyperlipidemia | 229 / 83% | 671 / 70% |
| pcos | pcos | 14 / 100% | 289 / 2% |

## Engine output totals (mean per patient)

- evidence: 33.9
- sustained trends: 3.2
- anomaly episodes: 12.5
- lab flags: 5.4
- relationships: 0.3
- co occurrences: 0.8
- timeline events: 48.5

Evidence statements that tripped the LLM guardrail patterns (should be 0): **0**

## Limitations

- The data is synthetic; agreement with recorded history is a consistency check, not clinical accuracy.
- Thresholds are documented demonstration heuristics, not clinical cut-offs.
- No confidence scores are produced (per business guidance).