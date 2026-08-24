FROM python:3.12-slim

RUN pip install --no-cache-dir \
      fastapi uvicorn fastmcp "psycopg[binary,pool]"

COPY app/ /app/
COPY db/schema.sql /app/schema.sql
WORKDIR /app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
