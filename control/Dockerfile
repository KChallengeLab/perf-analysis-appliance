# Onboard control panel — FastAPI + the docker CLI/compose plugin (to rebuild the stack) + git.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=UTC
ARG DOCKER_VERSION=27.3.1
ARG COMPOSE_VERSION=v2.29.7
ARG TARGETARCH=amd64

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Docker CLI (static) + compose plugin — no daemon; it drives the host daemon via the mounted socket.
RUN set -eux; \
    case "${TARGETARCH}" in amd64) A=x86_64;; arm64) A=aarch64;; *) A=x86_64;; esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${A}/docker-${DOCKER_VERSION}.tgz" -o /tmp/d.tgz; \
    tar -xzf /tmp/d.tgz -C /tmp; mv /tmp/docker/docker /usr/local/bin/docker; rm -rf /tmp/d.tgz /tmp/docker; \
    case "${TARGETARCH}" in amd64) C=x86_64;; arm64) C=aarch64;; *) C=x86_64;; esac; \
    mkdir -p /usr/local/lib/docker/cli-plugins; \
    curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${C}" -o /usr/local/lib/docker/cli-plugins/docker-compose; \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

RUN pip install "fastapi>=0.115,<1" "uvicorn>=0.30,<1" "pydantic>=2.7,<3"

WORKDIR /app
COPY app.py default_channels.json ./

EXPOSE 4267
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "4267"]
