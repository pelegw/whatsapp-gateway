FROM python:3.12-slim
WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
COPY cli ./cli
RUN pip install --no-cache-dir .
EXPOSE 8080
# Single worker on purpose: in-process rate limiter + SQLite single-writer.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
