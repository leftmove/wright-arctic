FROM python:3.14.8-slim-bookworm@sha256:ddec44f5ab04dd68aaee83d7dc9831c964a4bb9e950c89960e60760373fbf54f AS build

COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:9a853e40f2eb00f980d265a84fba919dc38ff17750197e7f13c055e4e2f12edd /uv /usr/local/bin/uv

ENV UV_PYTHON=/usr/local/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    CARGO_TARGET_DIR=/var/cache/cargo-target \
    PATH=/usr/local/cargo/bin:/usr/local/bin:$PATH

RUN printf 'APT::Sandbox::User "root";\n' > /etc/apt/apt.conf.d/00root \
    && apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/* \
    && curl --fail --silent --show-error --location https://static.rust-lang.org/rustup/archive/1.28.2/x86_64-unknown-linux-gnu/rustup-init --output /tmp/rustup-init \
    && echo "20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c  /tmp/rustup-init" | sha256sum --check \
    && chmod 755 /tmp/rustup-init \
    && /tmp/rustup-init --profile minimal --default-toolchain none -y \
    && rm /tmp/rustup-init

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY engine ./engine

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/usr/local/rustup \
    --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/var/cache/cargo-target \
    uv sync --locked --no-dev --no-editable --python /usr/local/bin/python3

FROM python:3.14.8-slim-bookworm@sha256:ddec44f5ab04dd68aaee83d7dc9831c964a4bb9e950c89960e60760373fbf54f

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=build /usr/local/bin/uv /usr/local/bin/uv
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/pyproject.toml /app/uv.lock /app/README.md ./
COPY app ./app

RUN chmod -R a+rX /app /usr/local/bin/uv

ENV UV_PYTHON=/usr/local/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_SYNC=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_CACHE_DIR=/tmp/uv-cache \
    PATH=/app/.venv/bin:/usr/local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PORT=8000

USER app

EXPOSE 8000

CMD ["sh", "-c", "exec uv run --no-sync --offline --no-dev --python /usr/local/bin/python3 python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
