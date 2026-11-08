UV ?= uv
BUN ?= bun
WORKER_UV ?= $(UV) tool run --from uv==0.13.4 uv

.DEFAULT_GOAL := help
.PHONY: help sync dev run stubs check-stubs check test format build worker-setup worker-build worker-dev worker-deploy

help:
	@printf '%s\n' \
	  'make sync         Install locked dependencies and build the engine' \
	  'make dev          Start the development server' \
	  'make run          Start the production server' \
	  'make stubs        Regenerate native Python type stubs' \
	  'make check-stubs  Verify generated stubs are current' \
	  'make check        Run Python, Rust, binding, and security checks' \
	  'make test         Run backend security regression tests' \
	  'make format       Format Python and Rust' \
	  'make build        Build the engine source distribution and wheel'
	@printf '%s\n' \
	  'make worker-setup Prepare the local WebAssembly build tools' \
	  'make worker-build Build native wheels and validate the Worker bundle' \
	  'make worker-dev   Serve the built Worker locally' \
	  'make worker-deploy Deploy the validated Worker and runtime secrets'

sync:
	$(UV) sync --locked

dev:
	$(UV) run fastapi dev

run:
	$(UV) run fastapi run

stubs:
	$(UV) run --directory engine cargo run --locked --features stubgen --bin generate_stubs
	$(UV) run --no-sync ruff check --fix --config pyproject.toml engine/python/engine/_core.pyi
	$(UV) run --no-sync ruff format --config pyproject.toml engine/python/engine/_core.pyi

check-stubs:
	@set -eu; \
	  task_stub_dir=$$(mktemp -d); \
	  trap 'rm -rf "$$task_stub_dir"' EXIT HUP INT TERM; \
	  $(UV) run --directory engine cargo run --locked --features stubgen --bin generate_stubs -- "$$task_stub_dir"; \
	  $(UV) run --no-sync ruff check --fix --config pyproject.toml "$$task_stub_dir"; \
	  $(UV) run --no-sync ruff format --config pyproject.toml "$$task_stub_dir"; \
	  diff -u engine/python/engine/_core.pyi "$$task_stub_dir/engine/_core.pyi"

check:
	$(UV) run ruff check
	$(UV) run ruff format --check
	$(UV) run mypy
	$(MAKE) test
	$(UV) run --directory engine cargo fmt --all -- --check
	$(UV) run --directory engine cargo clippy --locked --all-targets --all-features -- -D warnings
	$(MAKE) check-stubs
	$(UV) run python -m mypy.stubtest engine

test:
	$(BUN) install --cwd app/tests --frozen-lockfile
	$(UV) run python -m unittest discover -s app/tests

format:
	$(UV) run ruff check --fix
	$(UV) run ruff format
	$(UV) run --directory engine cargo fmt --all

build: stubs
	$(UV) build --package wright-arctic-engine --no-sources

worker-setup:
	$(WORKER_UV) run python -m app.deployment setup

worker-build:
	$(WORKER_UV) run python -m app.deployment build

worker-dev:
	$(WORKER_UV) run python -m app.deployment dev

worker-deploy:
	$(WORKER_UV) run python -m app.deployment deploy
