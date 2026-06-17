# personal-tools — dev tasks
#
# The session-coach Anki builder depends on genanki, which lives in a project
# venv (gitignored). `make venv` creates it with both the runtime and test deps,
# after which `make test` runs the whole suite through that interpreter.

VENV := session-coach/anki/.venv
PY := $(VENV)/bin/python

.PHONY: help venv test clean

help:
	@echo "make venv   - create the dev virtualenv (genanki + pytest)"
	@echo "make test   - run the full test suite (needs 'make venv' first)"
	@echo "make clean  - remove __pycache__ / *.pyc"

venv:
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements-dev.txt

test:
	$(PY) -m pytest

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
