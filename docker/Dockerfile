# Runs the existing `run_demo.py` entry point (3 simulated ECUs + the Edge
# Gateway) inside a container -- this image does not invent a new
# application entrypoint. See README.md's "Run with Docker" section and
# ARCHITECTURE.md's "Reproducibility, Docker & CI/CD" section for the
# full explanation.
#
# Python 3.11, matching this project's standardized runtime (see
# README.md's "Requirements" section and .github/workflows/ci.yml) --
# independent of whatever Python version happens to be installed on the
# machine building this image.
FROM python:3.11-slim

# Unbuffered stdout/stderr so this project's structured JSON logs (see
# edge_gateway/logging_config.py) show up immediately via `docker logs`
# instead of sitting in a buffer. PIP_NO_CACHE_DIR keeps the image
# smaller; it has no effect on the pinned versions actually installed.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, as their own layer: rebuilding after an application
# code change (below) does not force a full pip re-install.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Only what run_demo.py actually needs at runtime. Deliberately excluded:
# tests/ (this image is not how tests are run -- see the CI workflow and
# README.md's "CI/CD" section, which run pytest directly, not inside
# Docker), docs/, infra/ (Terraform, a separate tool, not part of the
# running application), and dashboard/ (does not exist yet -- Phase 10).
COPY common/ ./common/
COPY simulation/ ./simulation/
COPY edge_gateway/ ./edge_gateway/
COPY analyzer/ ./analyzer/
COPY run_demo.py .

# Non-root user. The Edge Gateway's SQLite buffer (edge_gateway/data/) is
# created at runtime by edge_gateway/buffer.py itself (it calls
# `Path.mkdir(parents=True, exist_ok=True)`), so the directory only needs
# to exist with the right ownership, not be pre-populated.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/edge_gateway/data \
    && chown -R appuser:appuser /app
USER appuser

# No EXPOSE: this container is an MQTT *client* (it connects out to
# Mosquitto or AWS IoT Core), not a server -- it has nothing for another
# host to connect to, so there is no port to publish. Mosquitto's own
# official image is what exposes 1883 (see docker-compose.yml).
#
# Configuration is entirely environment-variable-based, matching
# .env.example -- no secrets or connection details are baked into this
# image:
#   AWS_IOT_ENDPOINT / AWS_IOT_CA_PATH / AWS_IOT_CERT_PATH / AWS_IOT_KEY_PATH
#     -- set all four to publish to AWS IoT Core instead of local
#        Mosquitto; leave all unset (the default) to use local Mosquitto.
#        Certificate files must be bind-mounted into the container if used
#        (see docs/aws-setup.md) -- this image contains none.
#   ANTHROPIC_API_KEY (optional) -- enables the analyzer's advisory LLM
#     explanation layer (Phase 6). Unset by default; nothing in this
#     image requires it.
#
# IMPORTANT ARCHITECTURAL CONSTRAINT (see run_demo.py's own docstring and
# docs/edge-gateway-spec.md): python-can's "virtual" bus only shares CAN
# frames *within one OS process*. The 3 simulated ECUs and the Edge
# Gateway must therefore keep running together, as threads inside this
# one container/process, exactly as run_demo.py already does locally --
# splitting them into separate containers would give the gateway no bus
# traffic to read. Do not "fix" this by trying to split them.
CMD ["python", "run_demo.py"]
