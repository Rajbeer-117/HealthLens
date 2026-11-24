# HealthLens: AI Health Intelligence with a Large Language Model

**COMP 8967 Internship Project I · University of Windsor · Group 7**
**Business partner:** PranaChain (Abhishek Chakrala, Founder) · **Instructor:** Dr. Kalyani Selvarajah
**Team:** Rabiya Javed Farooq (AI Lead) · Haya Shah (Frontend & Validation) · Rajbeer Kaur Dutta (Data) · Babita Oli (Backend)

HealthLens connects a patient's labs, daily vitals, wearable data, medications, symptoms and menstrual cycles over time.
It finds patterns across several signals and explains them in plain language using a **Large Language Model (LLM)**.
It **never diagnoses** and never tells anyone to change medication. The Summary page shows an **educational risk score** per category from a model trained on the provided data (section 14), labelled as not a diagnosis; the chatbot itself does not give risk scores or predictions.

> Provided training data only (a mix of synthetic and real records) · Academic proof of concept · Not a medical device

> **Setup and run steps:** see [`HOW_TO_RUN.txt`](HOW_TO_RUN.txt). The quickest way is to double-click `run_with_llm.bat`.

---

## Contents
1. [Why an LLM, and why not on its own](#1-why-an-llm-and-why-not-on-its-own)
2. [Architecture](#2-architecture)
3. [Where the LLM is used in the app](#3-where-the-llm-is-used-in-the-app)
4. [How the LLM is used, step by step](#4-how-the-llm-is-used-step-by-step)
5. [Prompts](#5-prompts)
6. [Guardrails and safety](#6-guardrails-and-safety)
7. [Privacy](#7-privacy)
8. [Supported LLM providers](#8-supported-llm-providers)
9. [Transparency and audit](#9-transparency-and-audit)
10. [The analysis engine that feeds the LLM](#10-the-analysis-engine-that-feeds-the-llm)
11. [REST API](#11-rest-api)
12. [Testing and validation](#12-testing-and-validation)
13. [Code map](#13-code-map)
14. [Limitations and next steps](#14-limitations-and-next-steps)

---

## 1. Why an LLM, and why not on its own

At kickoff, PranaChain asked for a system that helps people *understand* their health information. It must not diagnose, must not create panic, must not automatically send people to the ER, and should not show a confidence score. LLM use is allowed if healthcare privacy is respected.

LLMs explain things well, but on their own they can invent facts, sound overconfident, or drift into diagnosis. So HealthLens uses a **hybrid design**:

| Layer | Job | Why |
|---|---|---|
| **Statistics engine** (transparent, deterministic) | *Finds* the patterns: trends, unusual readings, multi-signal scenarios | Results are reproducible, explainable and testable |
| **LLM** | *Explains* those findings in plain language and answers questions | Makes the results readable for patients |
| **Guardrails** | Check every LLM reply before anyone sees it | Keep the output grounded, non-diagnostic and calm |

The LLM never looks at raw data and never decides what is important. It only explains facts that the engine already computed and numbered (E1, E2, …).

---

## 2. Architecture

```
 ┌──────────────┐   ┌────────────────────┐   ┌───────────────────┐   ┌───────────────┐
 │ Patient Excel│──►│ Statistics engine  │──►│ Evidence register │──►│ De-identifier │
 │  (11 sheets) │   │ trends · anomalies │   │  E1 … En          │   │ no ID, no     │
 └──────────────┘   │ scenarios · quality│   │ fact+method+source│   │ calendar dates│
                    └────────────────────┘   └───────────────────┘   └──────┬────────┘
                                                                             ▼
 ┌──────────────┐   ┌────────────────────┐   ┌───────────────────┐   ┌───────────────┐
 │  Dashboard   │◄──│ Guardrail checker  │◄──│   LLM (Ollama /   │◄──│ Prompt builder│
 │ + audit log  │   │ pass → show        │   │ Claude / OpenAI / │   │ system rules +│
 └──────────────┘   │ fail → retry once  │   │ Gemini / Groq)    │   │ evidence+task │
                    │ fail again → safe  │   └───────────────────┘   └───────────────┘
                    │ template fallback  │
                    └────────────────────┘
```

**Stack:** Python 3 · Flask · pandas / NumPy · SQLite (local file `users.sqlite3`) · HTML/CSS/JavaScript · Chart.js.
The LLM is called over HTTP using Python's standard library (`urllib`), so no AI SDK has to be installed.

---

## 3. Where the LLM is used in the app

| # | Feature | Where to find it | What the LLM does |
|---|---|---|---|
| 1 | **HealthLens chatbot** | Round button at the **bottom-right of every page** (Summary, Profile, Documents) | Answers free-text questions about the selected patient, using only that patient's evidence. The conversation is kept while you move between pages |

The earlier *AI Health Summary* tab, the AI transparency panel and the *AI Integration* page were removed from the interface in the current layout. The LLM code that powered them (`llm.summarize`, `llm.test_connection`, the audit log) is still in `healthlens/llm.py` and covered by tests, so they can be brought back.

Each AI answer carries a label:

| Label | Meaning |
|---|---|
| `LLM-generated · passed guardrails` | Written by the LLM and accepted by every check |
| `Fallback template` | The LLM was unreachable or failed the checks twice, so a safe template is shown (with the reason) |
| `Safety response` | The question was blocked *before* reaching the LLM (diagnosis, medication change, acute symptoms) |
| `Template explanation (offline mode)` | No LLM is configured |

---

## 4. How the LLM is used, step by step

All of this is in [`healthlens/llm.py`](healthlens/llm.py).

1. **Analyse.** `engine.analyze_book()` turns the workbook into numbered evidence items, for example:
   `E7 (trend): Steps rose consistently from 4,372 to 5,884 steps/day (+34.6%) between month 1 of the record and month 12 of the record…`
2. **De-identify and build context.** `build_context()` keeps only the age band, sex, the scenario results and the evidence text, with relative dates.
3. **Prompt.** A fixed `SYSTEM_PROMPT` (the rules), plus the context, plus a task (`SUMMARY_TASK` or `ASK_TASK`) that asks for **JSON only**.
4. **Call the provider.** `call_llm()` sends the request to Ollama, Anthropic or an OpenAI-compatible API, with temperature 0.2 so replies stay consistent.
5. **Validate.** `validate()` parses the JSON, checks that every point cites evidence, rejects IDs that don't exist, and runs `guardrail_issues()` on every sentence.
6. **Retry once.** If anything fails, the LLM is told exactly which rules it broke and asked to rewrite.
7. **Fall back.** If it fails again, or the provider is down, `offline_summary()` / `offline_answer()` give a deterministic safe answer, and the page says why.
8. **Show and log.** The result goes to the dashboard together with its `trace`. `audit()` writes one privacy-safe line to `logs/ai_audit.jsonl`.

**Before step 3, for chat questions:** `screen_question()` catches diagnosis requests, medication-change requests and acute-symptom messages, and returns a fixed safe reply without calling the LLM.

### Example (summary request)

Excerpt of what is sent:
```
PERSON (de-identified): age band 30–39, sex Female, record length about 12 months.
SCENARIO CHECKS (counts of related signals, NOT risk scores):
- Women's health (PCOS): Several related signals (3 of 5 signal groups). Cycle regularity: 7/10 cycles longer than 35 days [E29]; ...
EVIDENCE:
E29 (cycle): 10 menstrual cycles were recorded between month 1 of the record and month 12 of the record: average length 38 days ...
```
Shape of the expected reply:
```json
{
  "overview": "This record covers about a year of labs, vitals, wearable data and cycle tracking...",
  "points": [
    {"title": "Longer menstrual cycles", "explanation": "Seven of ten recorded cycles were longer than 35 days ...", "evidence": ["E29"]}
  ],
  "questions": ["Could my cycle lengths be reviewed together with my lab results?"],
  "limitations": "Lab results come from only four draw dates, so lab trends rest on few points."
}
```

---

## 5. Prompts

- **System prompt** (`SYSTEM_PROMPT`). Eight mandatory rules:
  1. use only the given evidence;
  2. cite evidence IDs;
  3. no diagnosis (recorded conditions are called "recorded in the medical history");
  4. no risk, probabilities, confidence scores or predictions;
  5. no medication or treatment advice;
  6. no alarming or emergency wording;
  7. "moved together", never "caused";
  8. mention data limitations.
- **Summary task** (`SUMMARY_TASK`). Return JSON with `overview`, 3–5 `points` (each with `evidence`), `questions` and `limitations`. Prefer multi-signal patterns.
- **Ask task** (`ASK_TASK`). Return `{"answer", "evidence"}`. The user's question is wrapped in `<question>` tags, and the model is told to ignore any instructions inside them (basic prompt-injection protection).

The full prompts are in `healthlens/llm.py` (`SYSTEM_PROMPT`, `SUMMARY_TASK`, `ASK_TASK`).

---

## 6. Guardrails and safety

**Output checks.** LLM replies are rejected if they contain:

| Category | Example that is blocked |
|---|---|
| Diagnostic language | "You may have prediabetes", "consistent with hypertension" |
| Diagnosis claim | "diagnosed with…" |
| Risk / probability | "70% chance", "increased risk", "confidence score" |
| Prediction | "will lead to diabetes" |
| Emergency / alarm | "go to the emergency room", "dangerous", "critical" |
| Medication advice | "stop taking metformin", "increase your dose" |
| Causal claim | "low sleep is causing…" |
| Grounding | Citing an evidence ID that doesn't exist; a point with no evidence; invalid JSON |

Negated phrases such as "this is **not** a diagnosis" or "does **not** show that one caused the other" are allowed, so the checks don't produce false alarms.

**Input screening** (in `ask()`, before any LLM call):

| Question type | Response |
|---|---|
| "Do I have diabetes?" | Explains that HealthLens cannot diagnose and points to a healthcare professional |
| "Should I stop my medication?" | Refers the person to their prescriber or pharmacist |
| "I have chest pain" | Says HealthLens can't assess current symptoms and suggests contacting a professional or the local emergency number |

**Retry and fallback:** one corrective retry, then a template. Users never see an unchecked LLM reply.

---

## 7. Privacy

| Sent to the LLM | Never sent |
|---|---|
| Age band (e.g. 30–39), sex | Patient ID, name, exact age |
| Computed evidence sentences | Raw spreadsheet rows |
| Relative time ("month 7 of the record") | Calendar dates |
| Scenario check results | Family history, free-text clinical notes |

- With **Ollama**, the model runs on the same computer, so no data leaves the machine at all.
- The audit log records **no question text and no health values**.
- API keys live only in a local `.env` file, which is git-ignored and never committed.

---

## 8. Supported LLM providers

| Provider | Setting | Default model | Notes |
|---|---|---|---|
| **auto** (default) | `HEALTHLENS_LLM_PROVIDER=auto` | — | Uses Ollama if it is running, otherwise an API key from `.env`, otherwise offline |
| **Ollama** (local, free) | `ollama` | `llama3.2` (auto-detects installed models) | Recommended: private and free |
| **Anthropic Claude** | `anthropic` + `ANTHROPIC_API_KEY` | `claude-haiku-5-5` | Cloud API |
| **OpenAI-compatible** | `openai` + `OPENAI_API_KEY` + `OPENAI_BASE_URL` | `gpt-4o-mini` | Also works with **Google Gemini** and **Groq** (both have free tiers) |
| **Offline** | `offline` | template | No LLM; deterministic explanations |

To configure, copy `.env.example` to `.env` and edit it. Optional settings: `HEALTHLENS_LLM_MODEL`, `HEALTHLENS_LLM_TIMEOUT` (default 120 s) and `OLLAMA_HOST`.

---

## 9. Transparency and audit

- **Evidence IDs.** The engine still numbers every finding (`E1…En`) and the LLM must cite them; the chatbot hides the IDs from patients, and the guardrails reject any ID that does not exist.
- **Audit log** (`logs/ai_audit.jsonl`). For each chatbot call it records the time, user, patient ID, action, provider, model, result, whether the guardrails passed, and latency. No question text or health values are logged.

---

## 10. The analysis engine that feeds the LLM

The LLM is only as good as its evidence. The engine in `healthlens/` provides:

| Feature | Method |
|---|---|
| Unified health timeline | All 11 sheets merged into dated events, plus detected unusual readings |
| Sustained trends | Mann-Kendall trend test plus Theil-Sen slope on weekly averages (lab draws are used directly) |
| Unusual readings | Robust z-score (median/MAD) against the person's own previous 28 days, plus a minimum practical difference |
| Multi-signal unusual days | Two or more different signals unusual within ±1 day, plus symptoms within ±3 days |
| Lab flags | Compared with the reference range printed in each patient's own workbook |
| Signal relationships | Spearman correlation of weekly averages (shown as "moved together") |
| Medication context | Averages before vs after each medication start, and adherence (timing only, not cause) |
| Scenarios | **Metabolic**, **Cardiovascular**, **Women's health (PCOS)**: a count of related signal groups, not a risk score |
| Data quality | Coverage, gaps and blank values per source; nothing is imputed |

---

## 11. REST API

Login is required. POST requests need the `X-CSRF-Token` header that the page provides.

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/patients` | The signed-in user's uploaded patients |
| GET | `/api/patients/<id>/analysis` | Full engine output (evidence, trends, scenarios, timeline…) |
| GET | `/api/patients/<id>/observations` | Every measurement on the record, one row each, with its source document (`?measure=ldl` filters) |
| POST | `/api/patients/<id>/ask` | Chatbot answer to `{"question": "..."}`, with a `timing` breakdown |

The pages are `/summary`, `/profile` and `/documents` (`/dashboard` redirects to `/summary`). Documents are handled by `POST /documents/upload`, `POST /documents/drive`, `GET /documents/<id>/download` and `POST /documents/<id>/delete`. All are limited to the signed-in user's own data.

---

## 12. Testing and validation

- **`python -m pytest -q`** runs **242 automated tests**:
  - the statistics;
  - that every insight points to real evidence;
  - that the LLM context contains no patient ID or dates;
  - that guardrails block 10 unsafe sentences and allow safe ones;
  - question screening;
  - a **mock LLM** that first replies unsafely, to show the retry fixes it;
  - fallback when the LLM is unreachable;
  - the API and CSRF checks;
  - the final-demo changes (database storage, no demo data, Review button, chatbot timing, team dashboard and team access, SQL injection).
  - documents uploaded one by one: reading lab reports (PDF/Word/CSV, unit conversion, misread values), building the record document by document, risk levels.

  Test files by owner: `test_data_records.py` (Rajbeer), `test_healthlens.py` and `test_chatbot_speed.py` (Rabiya), `test_app_pages.py`, `test_security_and_team_access.py` and `test_document_by_document.py` (Babita), `test_pages_and_dashboard.py` (Haya); shared set-up in `tests/conftest.py`.
- **`python validate_scenarios.py`** runs the engine on all 900 provided patients and writes `validation/validation_summary.md`.

  It also checks the scenarios against recorded medical history. This is a consistency check, not a measure of accuracy, and the recorded history is never used to compute the scenarios:

  | Scenario | With related history: % showing signals | Without: % showing signals |
  |---|---|---|
  | Women's health (PCOS) | 100% | 2% |
  | Metabolic | 82% | 42% |
  | Cardiovascular | 83% | 70% |

  0 of the engine's evidence statements trigger the guardrails.

---

## 13. Code map

```
app.py                    Flask: auth, Summary / Profile / Documents pages, file + Google Drive import, chatbot API
healthlens/
  llm.py                  ★ LLM layer: providers, auto-detect, prompts, de-identified context,
                            guardrails, question screening, retry/fallback, trace, audit log
  engine.py               runs the full analysis → one JSON result
  analysis.py             trends, anomalies, lab ranges, relationships, medication context, data quality
  scenarios.py            metabolic / cardiovascular / PCOS multi-signal checks
  evidence.py             evidence format (E1…En); dashboard vs de-identified rendering
  timeline.py             unified timeline
  data.py                 workbook cleaning, time series, combining a patient's documents (merge_workbooks)
  extract.py              reads values out of each uploaded document (PDF/Word/CSV/Excel/photo)
  risk.py                 risk level, risk score, risk factors and missing inputs per category
  risk_model.py           the trained risk model (inputs, scoring, explanation); weights in risk_model.json
  record_rules.py         which documents are accepted, asked about or rejected; impossible readings; observations
templates/                summary.html, profile.html, documents.html, app_base.html (frame + chatbot), login/register
static/js/                charts.js (trend charts), chat.js (chatbot), documents.js (upload page)
tests/                    pytest suite (includes a mock LLM server)
validate_scenarios.py     900-patient validation
train_risk_model.py       trains and tests the risk model on the 900 patients
sample_documents/         8 single documents for one synthetic patient, to upload one by one
DOCUMENT_UPLOADS.txt      document-by-document uploads, accept / reject rules (name, Patient ID, sex, date of birth), risk levels
DATABASE_DESIGN.md        database tables, keys and the reasoning behind them
run_with_llm.bat          one-click start with Ollama (Windows)
.env.example              LLM configuration template
```

`patient_data.zip` (the business partner's training data) is not stored in this public repository. Place it next to `app.py`.

---

## 14. Documents uploaded one by one, and risk levels

Patients upload documents one at a time (a lab report, a blood-pressure export, a visit note). Each one is read in the back end (`healthlens/extract.py`), its readings are saved in `health_records` linked to the patient and the document, and the analysis combines all of them. The Summary page shows a **risk score (0–100), risk level and risk factors** for each category, even from one document, and names the missing inputs ("2 of 6 key inputs are missing, so this score may not be accurate").

**Trained model.** The risk score comes from one logistic-regression model per category, **trained on the business partner's 900 patient workbooks** (`train_risk_model.py`): inputs are measurements only (never medical history or medications), the answer key is the recorded medical history, 80% of patients train and 20% test. Test AUC on unseen patients: metabolic 0.82, cardiovascular 0.92 (the old hand-made rules: 0.72 and 0.59); women's health 1.00 but with only 3 test patients with PCOS, so not reliable. No confidence or uncertainty value is shown, as the business partner asked. Report: `validation/risk_model_report.md`. Details and the sample documents: `DOCUMENT_UPLOADS.txt`; database design: `DATABASE_DESIGN.md`.

## 15. Limitations and next steps

- Small local models (e.g. `llama3.2`, 3B) sometimes fail the checks and fall back. Larger models (`qwen2.5:7b`, Claude, Gemini) follow the rules more reliably.
- Guardrails use pattern rules. A second "LLM-as-judge" check could catch subtler phrasing.
- The thresholds are demonstration heuristics, not clinical cut-offs. The cardiovascular scenario separates patients weakly on the provided data; the trained risk model (section 14) improves on it.
- The provided data is a mix of synthetic and real records (business partner). The chatbot runs a local model (Llama 3.2 via Ollama), so no health data leaves the computer; a cloud LLM should be HIPAA-compliant, as the business partner recommended. Authentication is for a demo only: there is no patient-level authorization, rate limiting or encryption at rest.
- Next: multi-turn chat memory, clinician vs patient explanation modes, LLM evaluation on a labelled set of answers, and integration with PranaChain's EHR APIs.


## Update: category panel and 3-month data span

- **Left panel on the Summary page:** the 3 categories (**Metabolic**, **Cardiovascular**, **Women's health (PCOS)**) plus *All measures*. Each shows its status (e.g. "Some related signals", "2 of 6 signal groups"). Clicking a category shows its signal checks and filters the key readings, flags and charts to that category's measures.
- **3-month data span:** by default, the analysis (summary, categories, charts and the chatbot) uses only the **latest 3 months** of each record. A *Last 3 months / Full record* switch in the panel changes it, and the choice is remembered. Profile and medical history always show the full record.
- Code: `healthlens/data.py` (`trim_book`), `healthlens/engine.py` (`analyze_book(book, months=3)`), `app.py` (`CATEGORIES`, `SPANS`, `summary()`), `templates/_category_panel.html`, `templates/summary.html`, `static/css/style.css`; tests in `tests/test_app_pages.py`.
