# Project conventions

- Use uv for all Python work: `uv sync`, `uv add`, `uv remove`, `uv lock`,
  `uv run`, and `uv build`. Never use standalone pip, Poetry, or Python commands.
- Keep Python application code in `app/`; do not add Python files at the root.
- Keep Rust code, its toolchain configuration, and native packaging in `engine/`.
- Keep README.md limited to `# wright-arctic`; the user will write the README.
- Defer the test suite and test directories until the user asks for them.

## Architecture

- `app.main.create_app` constructs the FastAPI app. Register modular APIRouters
  there and keep request handlers thin.
- Put reusable behavior in `app/services/`, API models in `app/schemas.py`, and
  typed FastAPI dependency providers in `app/dependencies.py`.
- Use Pydantic models at data boundaries and pydantic-settings for configuration.
  Fully annotate functions and public interfaces. Keep strict mypy checks passing;
  do not hide missing types with Any or blanket ignores.
- Import native functions through the public package, e.g. `from engine import hello`.
  `engine._core` is the implementation detail. Explicitly re-export public symbols
  in `engine/python/engine/__init__.py`.
- Annotate new Rust exports for pyo3-stub-gen and run `make stubs`. The generated
  `_core.pyi` and `py.typed` must ship in the engine wheel. Do not manually edit
  generated stubs. Complex native types may need explicit Python type mappings.
- Rust builds invoked by uv use the engine directory. Run other Rust commands via
  `uv run --directory engine cargo ...` so rustup sees `engine/rust-toolchain.toml`
  and PyO3 uses the project's Python environment.

## Commands

- `uv sync` installs dependencies and compiles the Rust extension.
- `uv run fastapi dev` starts the development server at 127.0.0.1:8000 with reload.
- `uv run fastapi run` starts the production server. Configure deployment workers,
  networking, and process supervision for the eventual deployment environment.
- `uv run mypy` checks Python types; `uv run ruff check` checks Python lint rules.
- `make sync` synchronizes the environment using the committed lockfile.
- `make dev` and `make run` are aliases for the FastAPI commands above.
- `make stubs` regenerates and formats the native type stubs.
- `make check-stubs` checks generated stubs without modifying tracked files.
- `make check` runs Python and Rust quality checks, stub generation consistency,
  and runtime signature checks for native exports.
- `make format` formats Python and Rust and fixes supported Python lint issues.
- `make build` regenerates stubs and builds an engine source distribution and wheel
  into `dist/`. The FastAPI application runs from source and is not a wheel.
- Run `make check` and `make build` before finishing changes to the bindings.
- Python source changes reload during development. Rust changes are rebuilt on
  the next uv sync/run; restart the server to load the rebuilt native module.
- Use `uv lock --upgrade` for intentional Python dependency upgrades; commit uv.lock
  and engine/Cargo.lock. Keep pinned toolchain and CI versions current together.

## Configuration and secrets

- Copy `.env.example` to ignored `.env` for local settings. Process environment
  variables override dotenv values. Use the `WRIGHT_ARCTIC_` prefix.
- For mounted secrets, export `WRIGHT_ARCTIC_SECRETS_DIR` in the process environment.
  Files follow the settings names with that prefix, e.g. `wright_arctic_api_key`.
  Environment and dotenv values take precedence over mounted files.
- Keep secrets in SecretStr fields with excluded serialization and representation.
  Never log settings objects or unwrap secrets except at the point of use.
- Add required secrets as required settings when features need them; do not supply
  production fallback credentials. The initial optional API key is only plumbing,
  not an authentication mechanism.
- Never commit .env files, native binaries, virtual environments, or build outputs.
