FROM python:3.11-slim

WORKDIR /app

COPY shared/requirements.txt shared-requirements.txt
COPY report_service/requirements.txt service-requirements.txt
RUN pip install --no-cache-dir -r shared-requirements.txt -r service-requirements.txt

COPY shared/ ./shared/
COPY report_service/ ./report_service/

RUN mkdir -p /app/reports

ENV PYTHONPATH=/app
CMD ["uvicorn", "report_service.main:app", "--host", "0.0.0.0", "--port", "8004"]
