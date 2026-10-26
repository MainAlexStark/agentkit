# Компьютер агента (run_python / run_bash). Из этого образа сделаны три вещи:
#   * контейнеры разговоров — по одному на разговор, от uid 1000, без сети наружу;
#   * сервер песочницы (сервис sandbox) — создаёт и убирает эти контейнеры;
#   * egress — прокси с белым списком доменов, единственный выход контейнеров в интернет.
# См. docker-compose.yaml.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 MPLBACKEND=Agg

# procps — ps/kill для фоновых задач; git, curl — частые просьбы «склонируй», «скачай».
RUN apt-get update && apt-get install -y --no-install-recommends git curl procps fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# docker CLI — только для сервера песочницы; в контейнерах разговоров он бесполезен: сокета там нет.
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

# Что умеет агент «из коробки». Остальное он поставит сам (pip install — через egress),
# пакеты лягут в папку разговора и переживут перезапуск контейнера.
RUN pip install pandas numpy matplotlib openpyxl python-docx reportlab pillow pypdf xlsxwriter

# Ядро agentkit — ради agentkit.sandbox и agentkit.egress (тот же источник, что и у агента: vendor/ или git-тег).
COPY requirements.txt ./
COPY vendo[r] ./vendor
RUN pip install -r requirements.txt

# Тот же uid, что у агента: файлы в общих рабочих папках читают и пишут оба.
RUN useradd -m -u 1000 sandbox && mkdir -p /work && chown sandbox /work
USER sandbox
WORKDIR /work
EXPOSE 8090
CMD ["python", "-m", "agentkit.sandbox", "--root", "/work", "--port", "8090"]
