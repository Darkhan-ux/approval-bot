FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV WEB_HOST=0.0.0.0 WEB_PORT=8000
EXPOSE 8000
CMD ["python", "-m", "app"]
