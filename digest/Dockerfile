FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY digest.py .
COPY crontab /etc/cron.d/digest-cron

RUN apt-get update && apt-get install -y --no-install-recommends cron && \
    rm -rf /var/lib/apt/lists/* && \
    chmod 0644 /etc/cron.d/digest-cron && \
    crontab /etc/cron.d/digest-cron

CMD ["cron", "-f"]
