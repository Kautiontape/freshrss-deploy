FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

CMD ["gunicorn", "-b", "0.0.0.0:8000", "-w", "2", "--timeout", "30", "app:app"]
