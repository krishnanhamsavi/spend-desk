# Each target wraps the Python CLI, so `python -m spend_desk <cmd>` works without make.
PY ?= python

.PHONY: data load build build-sf test lint eval deploy

data:
	$(PY) -m spend_desk data

load:
	$(PY) -m spend_desk load

build:
	$(PY) -m spend_desk build --target duckdb

build-sf:
	$(PY) -m spend_desk build --target snowflake

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check src tests

eval:
	$(PY) -m spend_desk eval

deploy:
	$(PY) -m spend_desk deploy
