FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
COPY scripts ./scripts

RUN pip install --upgrade pip && pip install .

EXPOSE 8000

# Persist the SQLite store + summary on a mounted volume.
ENV DB_PATH=/data/remediation.db \
    SUMMARY_PATH=/data/summary.md

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
