# HealthLens risk model: training and test report

Trained on the patient workbooks provided by the business partner (patient_data.zip, a mix of synthetic and real records; 900 patients, 2026-11-22). Inputs are measurements only; the answer is whether the recorded medical history names a related condition. 80% of patients trained the model, 20% (never seen in training) tested it.

| Category | Train / test patients | Positives (test) | Test AUC (all data) | Test AUC, one source only | Old rule signals, test AUC | Moderate+ sensitivity / specificity |
|---|---|---|---|---|---|---|
| metabolic | 720 / 180 | 92 | **0.82** | labs 0.79, vitals 0.77, wearable 0.64 | 0.72 | 0.728 / 0.716 |
| cardiovascular | 720 / 180 | 46 | **0.92** | labs 0.83, vitals 0.89, wearable 0.83 | 0.59 | 0.761 / 0.828 |
| pcos | 378 / 95 | 3 | **1.00** | cycles 1.00, labs 1.00, vitals 0.78 | 0.58 | 1.0 / 0.989 |

AUC: 0.5 = no better than chance, 1.0 = perfect separation of patients with and without the condition.
Levels: score < 35 Low, 35–64 Moderate, ≥ 65 High.

## Limitations
- Training data: the results show how well the model separates the provided patients, not clinical accuracy.
- The answer key is the recorded history in the provided workbooks, not a confirmed diagnosis.
- Women's health: only 14 patients have PCOS in their history (3 in the test set), so its test result is not reliable and should not be presented as accurate.
- Educational score, not a diagnosis; the app says so next to every score.
