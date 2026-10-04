FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/regulations.db

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD python -c "import os,sqlite3; sqlite3.connect(os.environ['DB_PATH']).execute('SELECT 1')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
