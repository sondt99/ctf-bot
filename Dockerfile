FROM python:3.11-slim

WORKDIR /app

# Build deps for cryptography wheel
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persistent data directory for SQLite
RUN mkdir -p /app/data

# Non-root user for security
RUN useradd -r -s /bin/false botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python", "-m", "bot.main"]
