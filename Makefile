PYTHON = PYTHONPATH=. python3
export PYTHONPATH := .

.PHONY: up down test demo lint eval check

up:
	docker compose up -d --build || ($(PYTHON) -m uvicorn api.main:app --host 127.0.0.1 --port 8080 &)

down:
	docker compose down || true

test:
	$(PYTHON) -m pytest tests/ -q
	$(PYTHON) -m demo.eval

demo:
	$(PYTHON) -m demo.dashboard

eval:
	$(PYTHON) -m demo.eval

lint:
	$(PYTHON) -m ruff check domain clinic_adapter nlu dialogue api voice observability demo tests

check: lint test
