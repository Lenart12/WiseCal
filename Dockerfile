FROM python:3.14-slim-trixie AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Build project using uv
WORKDIR /app
COPY . /app/
RUN uv sync --locked --compile-bytecode --no-editable

# Final image
FROM python:3.14-slim-trixie

# Install chromium for playwright
# System + app deps (no Chrome deps — Playwright handles those)
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl bash openssl \
      ghostscript libopenjp2-7 \
      ffmpeg \
      gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad gstreamer1.0-nice \
      gstreamer1.0-tools \
      fonts-liberation fonts-dejavu-core \
  && rm -rf /var/lib/apt/lists/*


COPY --from=builder --chown=app:app /app/.venv /app/.venv
WORKDIR /app
RUN /app/.venv/bin/playwright install --with-deps chromium
COPY templates /app/templates
COPY *.py /app/

CMD ["/app/.venv/bin/waitress-serve", "--trusted-proxy=*", "--trusted-proxy-headers=x-forwarded-host x-forwarded-for x-forwarded-proto x-forwarded-port x-forwarded-by", "--call", "wisecal:create_app"]
