FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/manual_import/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY services/__init__.py /app/services/__init__.py
COPY services/manual_import /app/services/manual_import
COPY services/ingestion /app/services/ingestion
EXPOSE 8000
CMD ["uvicorn", "services.manual_import.api:app", "--host", "0.0.0.0", "--port", "8000"]
