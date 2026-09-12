# PersistentCoder sandbox image.
#
# Minimal, project-local dev toolchain for the CodingAgent:
#   - Python 3.12
#   - pytest
#   - git
#
# Runs as a NON-ROOT user (uid/gid 1000).
# The container is always started with hardening flags by
# app/sandbox/runner.py (cap-drop ALL, no-new-privileges, pids/mem/cpu
# limits). It is never privileged and never mounts docker.sock.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-sandbox.txt /tmp/requirements-sandbox.txt

RUN pip install --no-cache-dir \
        -r /tmp/requirements-sandbox.txt

RUN groupadd --gid 1000 sandbox \
    && useradd \
        --uid 1000 \
        --gid 1000 \
        --create-home \
        --shell /bin/sh \
        sandbox

USER 1000:1000

WORKDIR /workspace

CMD ["python", "--version"]
