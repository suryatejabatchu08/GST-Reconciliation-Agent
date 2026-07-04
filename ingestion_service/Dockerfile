FROM python:3.11-slim AS builder

WORKDIR /app

# Install system deps for lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2-dev libxslt-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY shared/requirements.txt shared-requirements.txt
COPY ingestion_service/requirements.txt service-requirements.txt
RUN pip install --no-cache-dir -r shared-requirements.txt -r service-requirements.txt

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY shared/ ./shared/
COPY ingestion_service/ ./ingestion_service/

ENV PYTHONPATH=/app
CMD ["uvicorn", "ingestion_service.main:app", "--host", "0.0.0.0", "--port", "8001"]
