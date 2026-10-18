If you are an agent, do *not* edit this file; it is exclusively to be edited manually.


# Philosophy

As you go along, be sure to always plan thoroughly ahead, with maintainability *always* in mind. You want to implement the task at hand, but you also want to significantly better the codebase along the way. More important than even writing good code, is creating good systems.

This philosophy should be how you approach all work for this project; maintainability is the north star. Use your own, independent and thorough judgement to push maintainability to the maximum, wherever, in whatever way is necessary, even pushing existing systems aside.

You should never need to patch fixes, create workarounds, nor make *any* decisions which fail to serve the long-term. Everything bends to maintainability. Be tenacious in this.

# Conventions

## Main

- Use uv for all Python work; no standalone Python, pip, or Poetry.
- Keep Python in `app/`; Rust, toolchain config, and native packaging in `engine/`.
  No root Python files.
- README.md must contain only `# wright-arctic`; the user owns it.
- Defer tests and test directories until requested.

## Architecture

- Construct the app and register modular APIRouters in `app.main.create_app`;
  keep handlers thin.
- Services: `app/services/`; API models: `app/schemas.py`;
  typed dependencies: `app/dependencies.py`.
- Religously abide to types, making Python effectively a static with Pydantic and friends. Seriously, add types wherever you can, wherever is reasonable.
- Use Pydantic at data boundaries and pydantic-settings for config. Fully type
  functions/public APIs; pass strict mypy without hiding gaps behind Any or blanket ignores.
- Import through `engine`; keep `engine._core` private. Explicitly re-export
  public symbols in `engine/python/engine/__init__.py`.
- Annotate Rust exports for pyo3-stub-gen; regenerate with `make stubs`, never
  hand-edit. Ship `_core.pyi` and `py.typed`; explicitly map complex native types as needed.
- Run Rust tools with `uv run --directory engine cargo ...` to select the
  engine's pinned toolchain and project Python.

## Commands

- `uv sync`: install dependencies/build engine; `make sync`: locked equivalent.
- `uv run fastapi dev` / `make dev`: reload at 127.0.0.1:8000.
- `uv run fastapi run` / `make run`: production; configure workers, networking,
  and supervision for deployment.
- `uv run mypy`: types; `uv run ruff check`: lint.
- `make stubs`: regenerate/format native stubs; `make check-stubs`: verify without edits.
- `make check`: Python/Rust quality, stub freshness, and native runtime signatures.
- `make format`: format both languages and fix supported Python lint issues.
- `make build`: regenerate stubs; engine sdist/wheel in `dist/`. App runs from source.
- Run `make check` and `make build` after binding changes.
- Python reloads automatically; Rust rebuilds on the next uv sync/run and needs a server restart.
- Upgrade dependencies with `uv lock --upgrade`; commit `uv.lock` and `engine/Cargo.lock`.
  Update pinned toolchain and CI versions together.

## Configuration and secrets

- Copy `.env.example` to ignored `.env`; use `WRIGHT_ARCTIC_` names.
  Priority: process environment > dotenv > mounted secrets.
- Export `WRIGHT_ARCTIC_SECRETS_DIR` in the process environment for mounted files
  named after prefixed settings, e.g. `wright_arctic_api_key`.
- Use SecretStr; exclude secrets from serialization/repr. Never log settings;
  unwrap secrets only at use.
- Required secrets must be required fields; no production fallback credentials.
  The optional API key is configuration, not authentication.
- Never commit .env files, native binaries, virtual environments, or build outputs.
