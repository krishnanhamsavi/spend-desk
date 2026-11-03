# Each target wraps the Python CLI, so `python -m spend_desk <cmd>` works without make.
PY ?= python

.PHONY: data load build build-sf forecast forecast-sf anomaly anomaly-sf recommend agent-eval sync test lint eval deploy

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

anomaly:
	$(PY) -m spend_desk anomaly --target duckdb

# Retrains SNOWFLAKE.ML.ANOMALY_DETECTION 6 times (about 0.15 credits) and refreshes results/.
anomaly-sf:
	$(PY) -m spend_desk anomaly --target snowflake --snowflake-ml

recommend:
	$(PY) -m spend_desk recommend --target duckdb

# Calls the Anthropic API (about $1.50 per run). Not part of `make eval`.
agent-eval:
	$(PY) -m spend_desk agent-eval

sync:
	$(PY) -m spend_desk sync

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check src tests

eval:
	$(PY) -m spend_desk eval

deploy:
	$(PY) -m spend_desk deploy
