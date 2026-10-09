# Includes Chromium for automatic interface previews.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1
CMD gunicorn app:app --workers 2 --threads 4 --timeout 300 --bind 0.0.0.0:${PORT:-8080}
