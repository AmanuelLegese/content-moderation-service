# One image for api, worker, provider and tests (compose picks the command).
FROM python:3.12-slim
# uv installs exactly the versions pinned in uv.lock, much faster than pip.
COPY --from=ghcr.io/astral-sh/uv:0.12.24 /uv /usr/local/bin/uv
# Install into /venv and put it on PATH; precompile .pyc; unbuffered so logs appear immediately.
ENV UV_PROJECT_ENVIRONMENT=/venv UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1 PATH="/venv/bin:$PATH"
WORKDIR /srv
# Dependencies first, code second: Docker reuses the cached dependency layer when only code changes.
# --frozen fails the build if uv.lock is out of date instead of silently resolving new versions.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY . .
