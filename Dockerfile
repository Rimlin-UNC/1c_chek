# ======================================================================
# Ямастер Чек — Docker-образ
# Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | info@ymaster.ru
# ======================================================================
FROM python:3.12-slim

LABEL vendor="ООО «Ямастер» (ymaster.ru)" \
      description="Ямастер Чек — система сканирования кассовых чеков и интеграции с 1С"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /opt/ymaster-check

# Системные зависимости (OpenCV headless не требует GUI-библиотек)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 libgl1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY run.py ./
COPY tests ./tests

# Данные монтируются как volume
VOLUME ["/opt/ymaster-check/data"]
ENV DATABASE_URL=sqlite:////opt/ymaster-check/data/ymaster_check.db

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
