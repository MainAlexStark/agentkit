# Образ агента. Ядро agentkit ставится из requirements.txt: из vendor/ (по умолчанию)
# или по git-тегу. Сборка не требует доступа к приватным репозиториям.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# git — для зависимости по git-тегу; curl — для healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# Сначала зависимости — слой кэшируется, пока не меняются requirements и ядро.
COPY requirements.txt ./
COPY vendo[r] ./vendor
RUN pip install -r requirements.txt

COPY app ./app
COPY evals ./evals
COPY mcp.json ./

ARG VERSION=docker
ENV AGENT_VERSION=${VERSION}

# Не от root: агент с инструментами файлов и shell не должен иметь прав на весь контейнер.
RUN useradd -m -u 1000 agent && mkdir -p /srv/data/workspaces && chown -R agent /srv/data
USER agent

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fsS http://localhost:8080/healthz || exit 1
# Число процессов — AGENT_WORKERS; больше одного — только вместе с AGENT_REDIS_URL.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips '*' --workers ${AGENT_WORKERS:-1}"]
