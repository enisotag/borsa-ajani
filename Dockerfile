FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Istanbul \
    PORT=7860 \
    ENABLE_HEALTH_SERVER=1 \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    libfreetype6 \
    fontconfig \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data /tmp/matplotlib && chmod -R 777 /app/data /tmp/matplotlib

EXPOSE 7860

CMD ["python", "main.py"]
