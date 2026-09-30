# Console de l'appliance onboard — FastAPI + WebSocket + le CLI docker/compose + git.
# Interface unique de gestion de la carte : pilotage, live, reglages, mise a jour.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=UTC
ARG DOCKER_VERSION=27.3.1
ARG COMPOSE_VERSION=v2.29.7

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Docker CLI (static) + compose plugin — no daemon; it drives the host daemon via the mounted socket.
RUN set -eux; \
    # Architecture lue par uname sur la machine de build. Le defaut TARGETARCH=amd64 installait
    # un binaire x86_64 sur cette carte ARM64 -> "Exec format error" au premier appel docker.
    A="$(uname -m)"; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${A}/docker-${DOCKER_VERSION}.tgz" -o /tmp/d.tgz; \
    tar -xzf /tmp/d.tgz -C /tmp; mv /tmp/docker/docker /usr/local/bin/docker; rm -rf /tmp/d.tgz /tmp/docker; \
    C="$(uname -m)"; \
    mkdir -p /usr/local/lib/docker/cli-plugins; \
    curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${C}" -o /usr/local/lib/docker/cli-plugins/docker-compose; \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

RUN pip install "fastapi>=0.115,<1" "uvicorn[standard]>=0.30,<1" "pydantic>=2.7,<3"

WORKDIR /app
COPY app.py index.html channels.html console.css default_channels.json ./
# Polices de la charte, servies par la carte : le poste du bord n'a pas forcement
# d'acces internet, et un repli silencieux sur Arial casserait l'identite.
COPY fonts ./fonts

EXPOSE 4267
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "4267"]
