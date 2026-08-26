FROM python:3.12-slim

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app/ /app/
COPY db/schema.sql /app/schema.sql
WORKDIR /app

# PYTHONPATH so the CLI importers can be run against a mounted archive with
#   docker exec <addon> python -m importers.apple_xml /share/archive/export.xml
ENV PYTHONPATH=/app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
