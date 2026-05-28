# Pinned to 3.12 to match the project's declared target (pyproject requires-python).
FROM python:3.12-slim

# - PYTHONDONTWRITEBYTECODE: no .pyc clutter in the image/volume
# - PYTHONUNBUFFERED: logs flush immediately so `docker compose logs` is live
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy only dependency metadata first so Docker caches the install layer and
# doesn't reinstall on every source change.
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
