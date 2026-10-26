"""HealthLens analysis and AI-explanation package (Deliverable 2).

Layers
------
data.py      -> reads a patient workbook into clean, dated pandas frames
evidence.py  -> the shared evidence format (E1, E2, ...) every insight points to
analysis.py  -> trends, anomalies, reference-range checks, relationships, data quality
scenarios.py -> multi-signal patterns for the metabolic, cardiovascular and PCOS scenarios
timeline.py  -> one longitudinal timeline built from every sheet
engine.py    -> runs everything and returns one JSON-ready result
llm.py       -> LLM explanation layer (Ollama / Claude / OpenAI-compatible / offline) with guardrails

Nothing in this package diagnoses, predicts risk or recommends treatment.
"""
from .engine import analyze_book  # noqa: F401
