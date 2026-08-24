FROM ${BUILD_FROM}

RUN pip install --no-cache-dir \
      fastapi uvicorn fastmcp "psycopg[binary,pool]"

COPY app/ /app/
WORKDIR /app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
