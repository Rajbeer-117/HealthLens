# HealthLens database design

HealthLens is used by **patients**. Each account belongs to one patient, who uploads their own documents one at a
time: a lab report, a blood-pressure export, a fitness-tracker export, a visit note. The database has to build up
that patient's record document by document. It also has to stay correct when documents arrive in any order, with or
without a Patient ID, and sometimes by mistake.

The design starts from what is true in this domain. Each table exists because of one of these facts.

## 1. What is true in the domain

| # | Fact | What follows for the database |
|---|---|---|
| 1 | An account belongs to one patient, and the patient uploads their own documents. | Whose record a document belongs to comes from the **account**, from the first upload. The record key is set by the account and never changes. |
| 2 | A Patient ID printed in a document is a label from whichever lab or clinic made it. One person has a different ID at each lab, and many documents (phone and device exports) have none. | IDs are **facts about documents**, stored in their own table (`patient_identifiers`). They are never the key of the record. A new ID is accepted only when the same name or date of birth shows it is the same person. |
| 3 | Name, sex and date of birth identify the patient. They come from the documents, and the first document that states each one sets it for the record. | They are stored with their **source document** (`patient_attributes`). |
| 4 | A document is evidence: the file stays exactly as uploaded. | `documents` keeps the original file and its fingerprint. Everything extracted from it points back to it, so deleting a document removes exactly what it contributed. |
| 5 | A measurement is one fact: this patient had this value, at this time, according to this document. When it was measured is not when it was uploaded. | `observations`: one row per measurement, with `measured_at` and the source `document_id`. |
| 6 | Some measures feed particular risk categories. | A catalogue of measures and which categories use them (`measures`, `measure_categories`). "Missing inputs" becomes a query. |
| 7 | A risk score is derived from the data available at that moment and from a model version. | `risk_assessments` keeps a snapshot each time the set of documents changes, with the model version and the inputs used and missing. |

## 2. Tables

```
users 1 ── 1 patients                       (one account = one patient; team accounts may hold several)
             │
             ├── n documents ── n health_records    raw rows as extracted (what the analysis engine reads)
             │        │
             │        ├────── n observations         one row per measurement ── measures ── measure_categories
             │        ├────── n patient_identifiers  Patient IDs found in documents
             │        └────── n patient_attributes   name / sex / date of birth stated in the document
             │
             └── n risk_assessments                       score snapshots per set of documents
```

| Table | Key columns | Purpose |
|---|---|---|
| `users` | id, username, email, password_hash | Login only. Health data lives elsewhere. |
| `patients` | id, user_id, patient_code, UNIQUE(user_id, patient_code) | The patient's record. `patient_code` is an internal key set by the account (not taken from a document). |
| `documents` | id, user_id, patient_id (= record key), kind, content, sha256, note | Every upload. `kind`: `patient` (accepted and analysed), `document` (stored only, no readable values). |
| `health_records` | document_id, patient_code, sheet, record_date, data (JSON) | **Raw layer**: every extracted row exactly as read. The analysis engine reads this, so nothing about the analysis changed. |
| `observations` | document_id, patient_code, measure_code, value, unit, measured_at, ref_low, ref_high | **Fact layer**: one row per measurement in a standard form, with the lab's own reference range. |
| `measures` | code (PK), name, unit, kind | Catalogue of measures, e.g. `ldl`, "LDL cholesterol", mg/dL, lab. |
| `measure_categories` | (measure_code, category) PK | Which risk categories use which measures. |
| `patient_identifiers` | patient_code, value, source_document_id, confirmed, UNIQUE(user_id, patient_code, value) | Patient IDs on the record (`confirmed` is kept for older databases; it is no longer set). |
| `patient_attributes` | patient_code, name (`name` / `sex` / `birth_date`), value, source (`document`), source_document_id | Which document said what about the patient. The first document that states a value sets it. |
| `risk_assessments` | patient_code, category, score, level, model_version, inputs_used, inputs_missing, factors, documents | Snapshot of each risk result for one set of documents. |

`team_admins`, `audit_events`, `chat_metrics`, `user_state` and `app_settings` are unchanged.

### Why both `health_records` and `observations`?

They are two layers of the same data, each with its own job.
- **`health_records` (raw):** keeps every row exactly as it was read, including columns HealthLens doesn't analyse. The analysis engine and the per-document summaries use it.
- **`observations` (facts):** holds the measurements in one standard shape. That makes questions like "every LDL value with its source" a simple query, and plausibility checks a single rule.

Both are written in the same transaction and deleted together with their document.

## 3. What happens to each upload (patient accounts)

Implemented in `healthlens/record_rules.py` (pure functions) and `ingest()` in `app.py`.

| Situation | Result |
|---|---|
| File can't be used (corrupt, empty, password-protected, wrong type, too large) | **Rejected**, with the reason |
| Shows a different **name, sex or date of birth** than the record, an age that doesn't fit the date of birth, or period data while the record is male | **Rejected**: "note.txt lists name Jane Doe, but 01_lab_report_2025-09-25.pdf says Maya Patel. Not added." |
| Shows a Patient ID **not seen on this record before**, and neither the name nor the date of birth matches | **Rejected**: "… shows Patient ID PC0999, but your record has PC0303 and the document does not show the same name or date of birth. Not added." |
| Shows a new Patient ID with the **same name or date of birth** (another lab's number) | **Accepted**, and the new ID is stored |
| No Patient ID, a known ID, or the first document | **Accepted** |
| Contains impossible single readings (glucose 9000 mg/dL, a reading dated in the future) | **Accepted without those readings**; the Documents page says how many were left out |
| No readable health values (e.g. a scanned PDF without text) | **Stored only**, so it can be downloaded |
| Same file, or the same readings, as an earlier upload | "Already uploaded"; nothing is added |

**Deleting a document** removes its raw rows, observations and stated attributes. If a Patient ID is no longer shown by any remaining document, that ID is removed too. So a wrong first upload cannot tie the record to someone else's ID.

## 4. Example queries

```sql
-- Latest LDL for a patient (by when it was measured, not when it was uploaded)
SELECT value, unit, measured_at FROM observations
WHERE user_id = ? AND patient_code = ? AND measure_code = 'ldl'
ORDER BY measured_at DESC LIMIT 1;

-- Cardiovascular inputs that have no measurements yet ("missing inputs")
SELECT m.name FROM measure_categories mc JOIN measures m ON m.code = mc.measure_code
WHERE mc.category = 'cardiovascular'
  AND NOT EXISTS (SELECT 1 FROM observations o WHERE o.user_id = ? AND o.patient_code = ?
                  AND o.measure_code = mc.measure_code);

-- How the cardiovascular score changed as documents were added
SELECT computed_at, score, level, documents FROM risk_assessments
WHERE user_id = ? AND patient_code = ? AND category = 'cardiovascular' ORDER BY id;

-- Every value with the document it came from (also: GET /api/patients/<id>/observations)
SELECT o.measure_code, o.value, o.measured_at, d.original_name
FROM observations o JOIN documents d ON d.id = o.document_id
WHERE o.user_id = ? AND o.patient_code = ?;
```

## 5. Upgrading an existing database

Nothing has to be done by hand. The new tables are created in `users.sqlite3` when the app starts. Documents stored by earlier versions get their observations, Patient IDs and attributes the first time their owner opens a page. Existing records keep their current key.

## 6. Limits and next steps

- **Issuer not recorded:** the lab or clinic that issued a Patient ID isn't detected yet, so IDs are compared as plain values.
- **Raw rows stored as JSON:** `health_records.data` is kept as JSON so the analysis engine didn't need rewriting. The next step is for the engine to read `observations` directly.
- **Names compared as text:** names are matched word by word (capitals, punctuation, order and middle names ignored). Nicknames or spelling variants ("Liz" / "Elizabeth") are treated as different names.
