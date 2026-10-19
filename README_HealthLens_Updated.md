# HealthLens — PranaChain Health Intelligence

**COMP 8967 · Fall 2026 · University of Windsor · Group 7**

HealthLens is a **Flask-based, full-stack proof of concept** for exploring longitudinal patient health data. It combines synthetic clinical records, laboratory measurements, daily vitals, wearable measurements, medication context, and available diagnosis history in a patient-friendly dashboard.

> **Educational prototype only.** HealthLens is not a medical device, diagnosis system, validated risk predictor, or emergency triage service. Its alerts and trends are descriptive demonstrations. **Use synthetic data only.**

## Current features

- **User accounts:** Registration, login, SQLite-backed account storage, password hashing, and logout.
- **Account dropdown:** Profile avatar with the signed-in username, email address, and logout action.
- **Patient selection:** Browse built-in synthetic patient workbooks or switch to imported patients.
- **Excel and ZIP upload:** Import a single `.xlsx` patient workbook or a `.zip` of workbooks through the dashboard.
- **Health charts:** Interactive Chart.js time-series visualization and metric overview charts for measurements available in the selected workbook.
- **Measurement alerts:** Flag latest readings outside configured *illustrative* adult reference ranges, with values, dates, and thresholds.
- **Explainable observations:** Compare first and last available measurements, with a reminder that these comparisons do not prove sustained trends.
- **Trend intelligence:** Compare the four most recent readings against earlier readings and display changes of at least 15% using an **unvalidated demo heuristic**.
- **Diagnosis and condition history:** Display timeline entries when matching structured history sheets are available; otherwise clearly indicate missing history.
- **Medication context and raw data preview:** Inspect medication history and source workbook sheets.

## Health intelligence workflow

1. **Understand:** Parse and organize available patient workbook sheets.
2. **Connect:** Display measurements alongside available profile, medication, and historical context.
3. **Detect:** Surface out-of-range latest values and descriptive longitudinal changes.
4. **Explain:** Show measurements, reference thresholds, dates, and comparison evidence.
5. **Ask (planned):** Identify missing information and generate context-sensitive follow-up questions.

**Not yet implemented:** Clinically validated multi-signal reasoning, personalized disease risk prediction, calibrated alert severity, or automated medical recommendations.

## Validation scenarios

The project is designed around three evaluation scenarios, **not three independently trained disease-prediction models**:

- Metabolic health / Type 2 diabetes risk
- Cardiovascular health / hypertension risk
- PCOS / women's health

Scenario-based evaluation and clinically grounded validation remain future work.

## Data

The PranaChain challenge describes **1,000 synthetic patient journeys** covering approximately 12 months. The bundled demo archive in this application contains **900 synthetic Excel workbooks**. Do not assume the demo bundle is the complete challenge dataset.

Patient workbooks may contain patient profile, medical history, medication history, lab results, daily vitals, wearable data, women's health data, clinical notes, or adherence records. **Actual sheets vary by patient.** The upload validator currently requires a `Patient Profile` sheet and at least one of `Lab Results`, `Vitals Daily`, or `Wearable Daily`.

[Open the patient workbook folder on Google Drive](https://drive.google.com/drive/folders/1uSkfm3tf4wdrcWtb_sA_tiwu3UVzSJq4)

**Google Drive is a download source only:** automatic Drive sync and Google OAuth are not included.

## Technology stack

| Layer | Technology |
| --- | --- |
| Backend | Python, Flask |
| Frontend | HTML, Jinja templates, CSS, JavaScript |
| Visualization | Chart.js (CDN) |
| Data processing | pandas, openpyxl |
| Authentication storage | SQLite |
| Input formats | `.xlsx`, `.zip` |

## Project structure

```text
HealthLens_Flask_Fullstack/
├── app.py                  # Flask routes, authentication, parsing, analysis
├── requirements.txt        # Python dependencies
├── patient_data.zip        # Bundled synthetic patient workbooks
├── templates/
│   ├── base.html
│   ├── login.html
│   ├── register.html
│   └── dashboard.html
├── static/
│   ├── css/style.css
│   └── js/app.js
├── users.sqlite3           # Created at runtime; do not commit
└── local_uploads/          # Created at runtime; do not commit
```

The `src/`, `notebooks/`, `tests/`, and `docs/` directories from the initial project proposal are **not part of this Flask application package**.

## Local installation

Requires a compatible Python 3 environment and internet access to load Chart.js from its CDN.

```bash
# From the HealthLens_Flask_Fullstack directory
python -m venv .venv
```

Activate the environment:

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
source .venv/bin/activate
```

Then install and start:

```bash
python -m pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5000** and register a demo account or log in.

## Upload and analyze a patient workbook

1. Download a synthetic patient `.xlsx` workbook (for example `PC0001.xlsx`) from the linked Google Drive folder, or prepare a ZIP of patient workbooks.
2. Log in and open the HealthLens dashboard.
3. Under **Upload a patient workbook**, choose the file and click **Upload & Analyze**.
4. Select **My uploaded patients** and choose the imported patient.
5. Review **Health Trends**, **Explainable Observations**, **Trend Intelligence**, **Diagnosis History**, **Medication Context**, and **Data Preview**.
6. Use **Built-in demo patients** to return to the bundled dataset.

Upload limits: **6 MB per workbook** and **120 workbooks per ZIP**. Imported workbooks are stored locally under `local_uploads/<user-id>/`.

## How warnings work

The prototype checks the latest available readings for a limited set of indicators: fasting glucose, HbA1c, LDL, HDL, systolic and diastolic blood pressure, and resting heart rate. It compares them with hard-coded illustrative reference values. It also compares recent and earlier readings to describe sufficiently large percentage changes.

**Limitations:** These thresholds are not personalized or clinically validated; measurement timing, units, patient history, medications, and other factors can change interpretation. No flag does **not** mean the patient is healthy. A flagged value does **not** establish a diagnosis.

## Privacy and security limitations

This is a local classroom demonstration, **not a production healthcare platform**. It does not provide production-grade patient authorization, CSRF protection across all actions, rate limiting, account verification, password reset, encryption-at-rest controls, or a data retention policy. Uploaded files are stored locally. Do not deploy the Flask debug server publicly or upload identifiable health records. Use a unique demo password and configure `HEALTHLENS_SECRET_KEY` for stable sessions.

## Development status

**Working prototype / active development.** The Flask UI, account system, workbook imports, charts, descriptive alerts, trend comparisons, and history views are implemented. Automated integration testing, robust security, multi-signal reasoning, and validation against the three challenge scenarios remain to be completed.

## Roadmap

- [x] Flask patient dashboard and authentication
- [x] Profile dropdown with username, email, and logout
- [x] Synthetic patient workbook browsing
- [x] `.xlsx` / `.zip` upload and analysis
- [x] Historical charts and descriptive warning cards
- [x] Diagnosis-history display when data exists
- [x] Basic longitudinal change detection
- [ ] Multi-signal reasoning across labs, vitals, wearables, and history
- [ ] Explicit missing-data questions and uncertainty handling
- [ ] Scenario-based evaluation and automated tests
- [ ] Accessibility, security hardening, and deployment preparation

---

**University of Windsor · COMP 8967 · Fall 2026**
