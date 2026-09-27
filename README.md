# PranaChain Health Intelligence

COMP 8967 – Fall 2026  
University of Windsor

## Project Overview

This project develops a proof-of-concept AI health intelligence framework
that connects historical clinical information with continuously changing
personal health data.

Rather than analyzing individual health measurements independently, the
system aims to identify meaningful longitudinal patterns by combining
clinical context, laboratory results, vitals, wearable data, medications,
and other relevant health information.

## Core AI Workflow

1. Understand – Structure available medical context
2. Connect – Relate ongoing health data to historical context
3. Detect – Identify trends, anomalies, and multi-signal patterns
4. Explain – Present the evidence supporting an identified pattern
5. Ask – Identify missing information that could improve understanding

## Validation Scenarios

The framework will be evaluated using three health scenarios:

- Metabolic Health / Type 2 Diabetes Risk
- Cardiovascular / Hypertension Risk
- PCOS / Women's Health

These are validation scenarios rather than separate disease-prediction
models.

## Dataset

The provided dataset contains 1,000 synthetic patient journeys covering
approximately 12 months of longitudinal health information.

Patient workbooks may contain:

- Patient profile
- Medical history
- Medication history
- Laboratory results
- Daily vitals
- Wearable data
- Women's health data
- Clinical notes
- Medication adherence

## Project Structure

- `src/` – Core processing and AI components
- `app/` – Prototype user interface
- `data/` – Dataset directories
- `notebooks/` – Exploration and experiments
- `tests/` – Automated tests
- `docs/` – Architecture, reports, and documentation

## Status

🚧 Initial Setup / Discovery Phase
