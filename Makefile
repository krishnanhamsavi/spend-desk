# Each target wraps the Python CLI, so `python -m spend_desk <cmd>` works without make.
PY ?= python

.PHONY: data load build build-sf forecast forecast-sf test lint eval deploy

data:
	$(PY) -m spend_desk data

load:
	$(PY) -m spend_desk load

build:
	$(PY) -m spend_desk build --target duckdb

build-sf:
	$(PY) -m spend_desk build --target snowflake

forecast:
	$(PY) -m spend_desk forecast --target duckdb

# Retrains SNOWFLAKE.ML.FORECAST 7 times (about 0.5 credits) and refreshes results/.
forecast-sf:
	$(PY) -m spend_desk forecast --target snowflake --snowflake-ml

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check src tests

eval:
	$(PY) -m spend_desk eval

deploy:
	$(PY) -m spend_desk deploy
