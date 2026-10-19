.PHONY: install test test-integration run

install:
	pip install -r requirements-dev.txt

test:
	pytest -q

test-integration:
	TEST_DATABASE_URL="$${TEST_DATABASE_URL:-postgresql://postgres:postgres@127.0.0.1:5432/dispatcher}" pytest -q

run:
	uvicorn app.main:app --host 0.0.0.0 --port $${PORT:-8000}
