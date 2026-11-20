# The sama sandbox image (docs/architecture.md, Section 16.2).
#
# One image for both providers: the Docker provider runs it as a container, and
# `python -m sama.sandbox.template` builds the E2B template `sama-runner` from it.
# It holds the runner and the Claude runtime only. It holds no secret: the runner gets a
# run token for each run.
#
# Build from the repository root:
#   docker build -f sandbox/Dockerfile -t sama-runner:dev .

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/sama/.venv

# Tools for the agent: a shell, git, and fast search.
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git ripgrep \
    && rm -rf /var/lib/apt/lists/*

# E2B builds this file too, and it does not support `COPY --from`, so pip installs uv.
RUN pip install --no-cache-dir uv==0.13.8

WORKDIR /opt/sama
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-default-groups --group runtime-claude --group runtime-openai \
    --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-default-groups --group runtime-claude --group runtime-openai \
    && pip uninstall --yes uv

# E2B templates use the user `user`. Create it here if the base image has none.
RUN id -u user >/dev/null 2>&1 || useradd --create-home --shell /bin/bash user \
    && mkdir -p /home/user/work \
    && chown -R user:user /home/user

USER user
WORKDIR /home/user/work
ENV PATH=/opt/sama/.venv/bin:$PATH

# No CMD. The Docker provider starts the container with `sleep infinity`, and E2B keeps its
# own agent running. The provider starts `python -m sama.runtime.runner` for each run.
