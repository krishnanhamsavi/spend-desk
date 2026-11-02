# Each target wraps the Python CLI, so `python -m spend_desk <cmd>` works without make.
PY ?= python

.PHONY: data test lint eval deploy

data:
	$(PY) -m spend_desk data

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check src tests

eval:
	$(PY) -m spend_desk eval

deploy:
	$(PY) -m spend_desk deploy
