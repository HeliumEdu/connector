.PHONY: all install nopyc clean test check run package package-plugin sync-assets

SHELL := /usr/bin/env bash
PYTHON_BIN ?= python
PROJECT_VENV ?= venv
LAMBDA_ARCH ?= aarch64
LAMBDA_PYTHON ?= 3.13

all: check test

venv:
	$(PYTHON_BIN) -m pip install virtualenv --user
	$(PYTHON_BIN) -m virtualenv $(PROJECT_VENV)

install: venv
	@( \
		source $(PROJECT_VENV)/bin/activate; \
		python -m pip install ".[dev]"; \
	)

nopyc:
	find . -name '*.pyc' | xargs rm -f || true
	find . -name __pycache__ | xargs rm -rf || true

clean: nopyc
	rm -rf build dist *.egg-info $(PROJECT_VENV)

test: install
	@( \
		source $(PROJECT_VENV)/bin/activate; \
		coverage run -m unittest discover -v && coverage report && coverage xml && coverage html; \
	)

check: install
	@( \
		source $(PROJECT_VENV)/bin/activate; \
		mypy heliumedu helium_connector && flake8; \
	)

run: install
	@( \
		source $(PROJECT_VENV)/bin/activate; \
		python -m helium_connector; \
	)

package:
	rm -rf build/lib build/lambda build/connector.zip
	$(PYTHON_BIN) -m pip install . \
		--target build/lambda \
		--platform manylinux2014_$(LAMBDA_ARCH) \
		--implementation cp \
		--python-version $(LAMBDA_PYTHON) \
		--only-binary=:all: \
		--upgrade
	cd build/lambda && zip -qr ../connector.zip . -x '*.pyc' '*/__pycache__/*'
	@echo "Built build/connector.zip"

package-plugin:
	$(PYTHON_BIN) bin/package-plugin.py build/helium-plugin.zip

sync-assets:
	bin/sync-assets.sh
