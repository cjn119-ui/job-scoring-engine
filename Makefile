PY ?= python3.11
.PHONY: setup run test
setup:
	$(PY) -m venv .venv && .venv/bin/pip install -e ".[dev]"
run:
	.venv/bin/python fixtures/make_fixtures.py
	.venv/bin/job-scoring run --boards config/boards.example.json --fixtures fixtures --now 2026-10-28T12:00:00Z --out out/report.md
	@cat out/report.md
test:
	.venv/bin/pytest -q
