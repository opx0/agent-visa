.DEFAULT_GOAL := help
.PHONY: help install fmt lint typecheck test e2e console demo all

help: ## list targets
	@grep -hE '^[a-z0-9-]+:.*##' $(MAKEFILE_LIST) | sed -e 's/:.*##/\t/' | expand -t 12

install: ## create the environment from uv.lock
	uv sync

fmt: ## format the tree
	uv run ruff format .

lint: ## check formatting and lint rules
	uv run ruff format --check .
	uv run ruff check .

typecheck: ## mypy strict over the package
	uv run mypy src

test: ## the fast suite, no network
	uv run pytest

e2e: ## the one live test against ego.ist
	LIVE=1 uv run pytest -m live

console: ## serve the holder console
	uv run agentvisa-console

demo: ## seed a demo passport, request and pass
	uv run agentvisa-demo

all: lint typecheck test ## everything CI runs, in CI order
