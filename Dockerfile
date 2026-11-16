FROM python:3.14.8-slim-bookworm AS build

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

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

RUN uv sync --locked --no-dev --no-editable --python /usr/local/bin/python3

FROM python:3.14.8-slim-bookworm

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/pyproject.toml /app/uv.lock /app/README.md ./
COPY app ./app

RUN /usr/local/bin/python3 -m compileall -q /app/app \
    && chmod -R a+rX /app

ENV PATH=/app/.venv/bin:/usr/local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PORT=8000

USER app

EXPOSE 8000

CMD ["/app/.venv/bin/python", "-m", "app"]
