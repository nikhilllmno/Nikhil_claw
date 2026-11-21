# The sama sandbox image (docs/architecture.md, Section 16.2).
#
# One image for both providers: the Docker provider runs it as a container, and
# `python -m sama.sandbox.template` builds the E2B template `sama-runner` from it.
# It holds the runner, the Claude runtime, and the coding agent CLIs (Claude Code and Codex). It
# holds no secret: the runner and each CLI get a run token for each run.
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
    && apt-get install -y --no-install-recommends bash ca-certificates curl git ripgrep xz-utils \
    && rm -rf /var/lib/apt/lists/*

# The coding agent CLIs (docs/architecture.md, Section 15.4): Claude Code and Codex, headless.
# They need Node.js 22. The versions are pinned: sama parses their event streams
# (`sama.coding.events`), and `sama.coding.agents` names the same versions. The image holds no
# key: the control plane gives each CLI the run token and the model proxy URL for one request.
ARG NODE_VERSION=22.23.5
ARG NODE_SHA256_X64=cdeb8670a0c81f51b4dc4ac61919f9b1ad917b304bf8221eb939570663d240f8
ARG NODE_SHA256_ARM64=2babb9eb5264860d540849f4b064a6fbdebcdc323657d9d382ce23d313fd5e09
ARG CLAUDE_CODE_VERSION=2.1.330
ARG CODEX_VERSION=0.171.0
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
      amd64) arch=x64; sum="$NODE_SHA256_X64" ;; \
      arm64) arch=arm64; sum="$NODE_SHA256_ARM64" ;; \
      *) echo "unsupported architecture"; exit 1 ;; \
    esac; \
    file="node-v${NODE_VERSION}-linux-${arch}.tar.xz"; \
    curl -fsSLo "/tmp/$file" "https://nodejs.org/dist/v${NODE_VERSION}/$file"; \
    echo "$sum  /tmp/$file" | sha256sum -c -; \
    tar -xJf "/tmp/$file" -C /usr/local --strip-components=1 --no-same-owner; \
    rm -f "/tmp/$file"; \
    npm install --global --no-fund --no-audit \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}"; \
    npm cache clean --force; \
    claude --version; \
    codex --version

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
