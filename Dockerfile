# deal-hunter in a container: `run` (the monitor) and `web` (the dashboard)
# from one image. Settings, secrets and state all come from outside it; see
# "Run in a container" in README.md for the mounts and environment.
#
# Only Playwright's headless Chromium is installed: in a container nothing
# can show a login window, so the monitor runs with
# facebook.require_session = true and a session saved by `deal-hunter login`
# on a desktop.

FROM ghcr.io/astral-sh/uv:0.11.26@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5 AS uv

FROM docker.io/library/python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

# Bytecode is compiled once at install (UV_COMPILE_BYTECODE); nothing is
# written at runtime, which suits a read-only root filesystem.
ENV PYTHONDONTWRITEBYTECODE=1 \
    DISABLE_PYNPUT=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    PATH=/opt/venv/bin:$PATH

WORKDIR /src

# Dependencies first (cached until uv.lock changes), hash-checked from the lock.
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
# The tracing extra (OpenTelemetry) is installed but off: nothing is sent unless
# OTEL_EXPORTER_OTLP_ENDPOINT is set (README: "Tracing").
RUN uv sync --locked --no-dev --no-install-project --extra tracing

# Chromium's system libraries (apt) and the headless shell, at the Playwright
# version uv.lock pins.
RUN playwright install-deps chromium-headless-shell \
 && playwright install --only-shell chromium \
 && rm -rf /var/lib/apt/lists/*

COPY src ./src
# The venv now holds its own copy of the package; drop uv and the sources.
RUN uv sync --locked --no-dev --no-editable --extra tracing \
 && rm /usr/local/bin/uv \
 && cd / && rm -rf /src

# A fixed non-root user. Everything it writes goes to mounted volumes:
# ~/.ai-marketplace-monitor (aimm's cache and log), /session (the saved
# Facebook login) and the working directory (the generated aimm config,
# page dumps). The image itself can be mounted read-only.
# The state directories exist in the image so a new Docker volume mounted on
# them starts out owned by this user.
RUN useradd --uid 1000 --user-group --home-dir /home/hunter --create-home hunter \
 && mkdir -p /home/hunter/.ai-marketplace-monitor /session /work \
 && chown hunter:hunter /home/hunter/.ai-marketplace-monitor /session /work \
 && chmod 0700 /home/hunter/.ai-marketplace-monitor /session
USER 1000:1000
ENV HOME=/home/hunter
WORKDIR /work

EXPOSE 8740
ENTRYPOINT ["deal-hunter"]
CMD ["--help"]
