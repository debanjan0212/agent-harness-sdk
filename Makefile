.PHONY: dev check test lint fmt crewai demo serve up
dev:
	pip install -e ".[all,dev]"
lint:
	ruff check .
	ruff format --check .
fmt:
	ruff check --fix .
	ruff format .
test:
	pytest -q
check: lint test
crewai:
	python -m venv .venv-crewai && .venv-crewai/bin/pip install -q -e ".[crewai,dev]" && .venv-crewai/bin/pytest -q tests/test_crewai.py
demo:
	cd examples && python -m incident_commander demo --scenario bad-deploy
serve:
	cd examples && python -m incident_commander serve
up:
	docker compose -f deploy/docker-compose.yaml up --build
