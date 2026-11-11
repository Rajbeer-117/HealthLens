# Run from the full HealthLensDemo project directory
# Install dependencies: python -m pip install -r requirements.txt
# Full suite:
python -m pytest -v --tb=short
# Coverage:
python -m pytest --cov=app --cov=healthlens --cov-report=term-missing --cov-report=html
# Open report in PowerShell: start htmlcov\index.html
