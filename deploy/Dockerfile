# The sama control plane image (docs/architecture.md, Section 19).
#
# One process runs the API, the tool server, the model proxy, the worker, the monitor, and the
# Telegram gateway: `python -m sama.controlplane`. The Fly release command runs the database
# migrations with the same image. The image holds no secret: Fly gives them as environment
# variables.
#
# Build from the repository root:
#   docker build -f deploy/Dockerfile -t sama-controlplane:dev .

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/sama/.venv \
    PATH=/opt/sama/.venv/bin:$PATH \
    SAMA_BUILTIN_SKILLS_DIR=/opt/sama/skills \
    SAMA_CONNECTORS_DIR=/opt/sama/connectors/catalog

RUN pip install --no-cache-dir uv==0.13.8

WORKDIR /opt/sama
COPY pyproject.toml uv.lock README.md ./
# The control plane does not run an agent runtime: the runner does, in the sandbox. It needs
# only the E2B client, and the database drivers of the gateway (M4.13).
RUN uv sync --frozen --no-dev --no-default-groups --group sandbox-e2b --group databases --no-install-project
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
COPY skills ./skills
COPY connectors ./connectors
RUN uv sync --frozen --no-dev --no-default-groups --group sandbox-e2b --group databases --no-editable \
    && pip uninstall --yes uv \
    && useradd --system --no-create-home sama

USER sama
EXPOSE 8080
CMD ["python", "-m", "sama.controlplane"]
