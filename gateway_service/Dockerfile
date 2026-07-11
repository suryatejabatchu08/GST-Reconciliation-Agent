FROM python:3.11-slim

WORKDIR /app

COPY shared/requirements.txt shared-requirements.txt
COPY gateway_service/requirements.txt service-requirements.txt
RUN pip install --no-cache-dir -r shared-requirements.txt -r service-requirements.txt

COPY shared/ ./shared/
COPY gateway_service/ ./gateway_service/

ENV PYTHONPATH=/app
CMD ["uvicorn", "gateway_service.main:app", "--host", "0.0.0.0", "--port", "8080"]
