# Песочница агента (run_python / run_bash): Python с библиотеками для таблиц, графиков и документов.
# Без сети и без секретов сервиса — см. сервис sandbox в docker-compose.yaml.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 MPLBACKEND=Agg

RUN apt-get update && apt-get install -y --no-install-recommends git fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Что умеет агент в песочнице. Добавляйте нужное проекту — песочница изолирована.
RUN pip install pandas numpy matplotlib openpyxl python-docx reportlab pillow pypdf xlsxwriter

# Ядро agentkit — ради agentkit.sandbox (тот же источник, что и у агента: vendor/ или git-тег).
COPY requirements.txt ./
COPY vendo[r] ./vendor
RUN pip install -r requirements.txt

# Тот же uid, что у агента: файлы в общих рабочих папках читают и пишут оба.
RUN useradd -m -u 1000 sandbox && mkdir -p /work && chown sandbox /work
USER sandbox
WORKDIR /work
EXPOSE 8090
CMD ["python", "-m", "agentkit.sandbox", "--root", "/work", "--port", "8090"]
