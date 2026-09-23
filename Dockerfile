FROM python:3.14-slim-trixie AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Build project using uv
WORKDIR /app
COPY . /app/
RUN uv sync --locked --compile-bytecode --no-editable

# Final image
FROM python:3.14-slim-trixie

COPY --from=builder --chown=app:app /app/.venv /app/.venv
WORKDIR /app
COPY templates /app/templates
COPY *.py /app/

CMD ["/app/.venv/bin/waitress-serve", "--trusted-proxy=*", "--trusted-proxy-headers=x-forwarded-host x-forwarded-for x-forwarded-proto x-forwarded-port x-forwarded-by", "--call", "wisecal:create_app"]
